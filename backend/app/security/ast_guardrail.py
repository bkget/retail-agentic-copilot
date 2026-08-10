"""AST-level SQL guardrail.

The agent never executes the raw text an LLM produces. This module parses it with
sqlglot, validates the AST against a strict allow-list, and returns a re-serialized
SQL string built from the validated AST - never the model's original text. That
re-serialization step matters: it's what prevents parser-differential attacks (text
that sqlglot and Postgres would tokenize differently).

Two independent layers, both required:
  1. Shape check: the top-level statement must be a SELECT (optionally wrapped in a
     WITH). This rejects DROP/ALTER/SET/INSERT/UPDATE/DELETE/COPY/... outright.
  2. Full-tree walk: catches the same forbidden statement types when they're nested
     inside a CTE of an otherwise SELECT-shaped statement (e.g.
     `WITH x AS (DELETE FROM t RETURNING *) SELECT * FROM x`), and validates every
     table reference and every function call against explicit allow-lists.

A note on the function allow-list: sqlglot represents known SQL functions (SUM, CAST,
EXTRACT, ...) as dedicated `exp.*` node classes, not `exp.Anonymous` - only functions
sqlglot doesn't recognize come through as `exp.Anonymous`. A check that only inspects
`exp.Anonymous.name` (as a naive first pass might) never actually enforces the
allow-list against any function sqlglot recognizes. `_function_name` below handles
both cases: the node's own name for Anonymous, `sql_name()` for everything else.

A second note, found the hard way: sqlglot's `exp.And`/`exp.Or`/`exp.Xor` (the boolean
`AND`/`OR`/`XOR` connectors in a WHERE clause) are themselves subclasses of `exp.Func` -
a compound `WHERE a = 1 AND b = 2` was being rejected outright with "Function 'AND' is
not in the allow-list" the first time any generated query had two WHERE conditions.
`exp.Connector` (their shared base) is excluded from the function-allowlist check below
for exactly this reason: it's pure boolean composition, not a callable function that
needs allow-listing.
"""

from __future__ import annotations

import sqlglot
from sqlglot import exp

DIALECT = "postgres"

ALLOWED_TABLES: frozenset[str] = frozenset({"mv_sales_analysis", "mv_sales_daily_rollup"})
ALLOWED_SCHEMA = "public"

ALLOWED_FUNCTIONS: frozenset[str] = frozenset({
    "SUM", "AVG", "COUNT", "MIN", "MAX", "ROUND",
    "COALESCE", "NULLIF", "CAST", "LOWER", "UPPER",
})

# Node types that can never appear anywhere in the tree, including nested inside a CTE.
# exp.Set is deliberately included: SET statement_timeout/search_path/... would let a
# generated query defeat the DB-level session hardening (see db/init/03_roles.sh) or
# redirect unqualified table names to a different schema.
FORBIDDEN_NODE_TYPES: tuple[type[exp.Expression], ...] = (
    exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create, exp.Alter,
    exp.Command, exp.Set, exp.Merge, exp.Copy, exp.Grant,
)

MAX_ROW_LIMIT = 500


class GuardrailViolation(ValueError):
    """Raised when generated SQL fails AST validation. Message is safe to surface to the LLM
    for a self-correction retry - it names the offending construct, not internal detail."""


def _function_name(node: exp.Func) -> str:
    if isinstance(node, exp.Anonymous):
        return (node.name or "").upper()
    return node.sql_name().upper()


def _table_identity(table: exp.Table) -> tuple[str, str]:
    """Returns (schema, name), defaulting an unqualified table's schema to 'public' -
    matching Postgres's own default search_path behavior for this database."""
    schema = (table.db or ALLOWED_SCHEMA).lower()
    name = (table.name or "").lower()
    return schema, name


def validate_and_reserialize_sql(raw_sql: str) -> str:
    statements = sqlglot.parse(raw_sql, read=DIALECT)
    statements = [s for s in statements if s is not None]
    if len(statements) != 1:
        raise GuardrailViolation(
            f"Expected exactly one SQL statement, found {len(statements)}. "
            "Multi-statement input is rejected."
        )

    expression = statements[0]

    # Shape check: allow a bare SELECT, or a WITH whose final projection is a SELECT.
    # (sqlglot represents `WITH ... SELECT ...` as an exp.Select node with a `with`
    # arg, not as a separate top-level exp.With node, so checking exp.Select covers
    # both cases here.)
    if not isinstance(expression, (exp.Select, exp.Union)):
        raise GuardrailViolation(
            f"Statement must be a SELECT; got '{type(expression).__name__}'."
        )

    for node in expression.walk():
        node = node[0] if isinstance(node, tuple) else node

        if isinstance(node, FORBIDDEN_NODE_TYPES):
            raise GuardrailViolation(
                f"Forbidden SQL construct detected: '{type(node).__name__}'."
            )

        if isinstance(node, exp.Func) and not isinstance(node, exp.Connector):
            name = _function_name(node)
            if name not in ALLOWED_FUNCTIONS:
                raise GuardrailViolation(f"Function '{name}' is not in the allow-list.")

    for table_node in expression.find_all(exp.Table):
        schema, name = _table_identity(table_node)
        if schema != ALLOWED_SCHEMA or name not in ALLOWED_TABLES:
            raise GuardrailViolation(
                f"Table '{schema}.{name}' access denied - not in the semantic layer allow-list."
            )

    existing_limit = expression.args.get("limit")
    if existing_limit is None:
        expression = expression.limit(MAX_ROW_LIMIT)
    else:
        limit_value = existing_limit.expression
        if not (isinstance(limit_value, exp.Literal) and limit_value.is_int
                and int(limit_value.this) <= MAX_ROW_LIMIT):
            expression = expression.limit(MAX_ROW_LIMIT)

    return expression.sql(dialect=DIALECT)
