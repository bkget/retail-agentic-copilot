"""The only LLM-dependent step in this pipeline is NL -> candidate SQL text. Everything
downstream (validation, execution, metric computation, narrative) is deterministic Python -
see app/agent/orchestrator.py and app/agent/narrative.py.

LLMProvider is the seam a real Gemini/ADK implementation plugs into later without touching
the orchestrator, tests, or anything else. MockLLMProvider below is a genuine rule-based NL
router against the real semantic layer (not a canned-response stub) - enough to drive the
vertical slice and the eval harness end-to-end before a Gemini key exists.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum

from app.schema.catalog import SchemaCatalog


@dataclass(frozen=True)
class ConversationTurn:
    question: str
    sql: str | None
    row_count: int | None
    # Cached so "explain the above result" can replay it without re-running a query -
    # None for conversational/clarification turns, where there's nothing to cache.
    narrative_text: str | None = None


class Intent(str, Enum):
    GREETING = "greeting"
    HELP = "help"
    CLARIFICATION_NEEDED = "clarification_needed"
    EXPLAIN_PREVIOUS = "explain_previous"
    DATA_COVERAGE = "data_coverage"
    QUERY = "query"


@dataclass(frozen=True)
class IntentResult:
    intent: Intent
    reply: str | None = None  # canned/generated text for GREETING/HELP
    clarifying_question: str | None = None  # for CLARIFICATION_NEEDED


class LLMProvider(ABC):
    @abstractmethod
    async def classify(self, question: str, history: list[ConversationTurn]) -> IntentResult:
        """Decides whether `question` needs a database query at all before any SQL
        generation is attempted - greetings and "what can you do" style questions
        should never reach generate_sql, and a genuinely vague question ("revenue")
        should get a clarifying question back instead of a guessed default."""

    @abstractmethod
    async def generate_sql(
        self,
        question: str,
        catalog: SchemaCatalog,
        history: list[ConversationTurn],
        error_feedback: str | None = None,
    ) -> str:
        """Returns candidate SQL text. Never trusted as-is - always passed through
        app.security.ast_guardrail before execution. `error_feedback`, when set, is the
        guardrail/DB error from a previous failed attempt in the same turn, for
        self-correction. Only called when classify() returned Intent.QUERY.

        Takes the structured `catalog` (not a pre-rendered prompt string) so a
        provider can do exact/programmatic matching against `catalog.enum_hints` -
        MockLLMProvider needs this for named-value filtering (see
        _extract_named_filters). A provider that wants a flat prompt string (Gemini)
        calls `catalog.as_prompt_context()` itself."""


class LLMGenerationError(RuntimeError):
    pass


_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
_TOP_N_RE = re.compile(r"\btop\s+(\d+)\b", re.IGNORECASE)


def _word_in(keyword: str, text: str) -> bool:
    """Whole-word match (allowing a trailing "s" for regular plurals), not substring -
    plain `in` checks are exactly how "country" (contains "count") got routed to
    COUNT(*) and "average order value" (contains "order") got routed to a transaction
    count instead of AVG. Every keyword lookup in this router goes through this, not
    `in`, for that reason."""
    return re.search(rf"\b{re.escape(keyword)}s?\b", text) is not None


# Ordered by priority, most specific first: a question mentioning both "payment" and
# "bank" (e.g. "revenue by payment bank") should resolve to the more specific
# payment_bank column, not the generic payment_type one - so "bank" must be checked
# before "payment". Order matters throughout this list, not just for that pair.
_DIMENSION_KEYWORDS: list[tuple[str, str]] = [
    ("bank", "payment_bank"),
    ("division", "store_division"),
    ("region", "store_division"),
    ("district", "store_district"),
    ("supplier", "item_supplier"),
    ("country", "item_manufacturer_country"),
    ("countries", "item_manufacturer_country"),  # irregular plural, "s?" doesn't cover it
    ("item", "item_name"),
    ("product", "item_name"),
    ("payment", "payment_type"),
    ("quarter", "sale_quarter"),
    ("month", "sale_month"),
    ("year", "sale_year"),
]

# Columns only present on the row-level Tier 2 view (mv_sales_daily_rollup's grain is
# sale_date/sale_year/sale_month x store_division/store_district x item_name only - no
# payment or quarter columns). Their presence in the question forces Tier 2 even if a
# Tier-1-satisfiable dimension (division/month/...) is also present.
_TIER2_ONLY_DIMENSIONS = {
    "item_supplier", "item_manufacturer_country", "payment_bank",
    "payment_type", "sale_quarter",
}

# --- Intent classification word lists -----------------------------------------------

_GREETING_WORDS = (
    "hi", "hello", "hey", "good morning", "good afternoon", "good evening",
    "thanks", "thank you", "bye", "goodbye",
)

_HELP_PHRASES = (
    "what can you", "what can i ask", "what kind of questions", "what columns",
    "tell me about yourself", "help",
    # "what kind of data" / "what data do you have" asks about queryable *domains*
    # (revenue, divisions, items, ...) - the same answer as "what can I ask", not a
    # question about the year range (that's Intent.DATA_COVERAGE, a different thing).
    # Without this, "what kind of data you've access to?" fell all the way through to
    # the generic scope-limitation clarification instead of actually answering.
    "what kind of data", "kind of data", "type of data", "what data do you have",
    # Bare, highly domain-specific words: "columns"/"questions" essentially never occur
    # in a genuine sales question, but do in real phrasings of "what can I ask" that
    # don't happen to match the literal multi-word phrases above verbatim - e.g. "what
    # ARE THE columns..." doesn't contain the literal substring "what columns".
    "columns", "questions",
)

# A follow-up referencing the *previous* answer ("explain the above result in plain
# english") - handled by replaying the cached narrative from history, not by running a
# new (unrelated) query. See MockLLMProvider.classify().
_EXPLAIN_PREVIOUS_PHRASES = (
    "explain", "in plain english", "summarize", "what does that mean",
    "what does this mean", "elaborate",
)

# Deliberately fuller phrases, not bare "which year"/"what year": a bare 2-word trigger
# would misfire on a real analytical question like "what year had the highest revenue?"
# (an answerable ranking query, not a meta-question about data coverage).
_DATA_COVERAGE_PHRASES = (
    "data you have access", "years of data", "date range", "which years",
    "what years", "years available", "time period do you have",
)

# "Available"/"list" without a ranking word ("top N") signals "show me the distinct
# values", not "rank these by a metric" - see _is_listing_intent / _build_listing_sql.
_LISTING_TRIGGER_WORDS = ("available",)

# Words that signal "this question is about a metric" - necessary but not sufficient
# for treating the question as answerable: a bare "revenue" has a metric signal but no
# qualifying context, and gets a clarifying question rather than a guessed default.
_METRIC_SIGNAL_WORDS = (
    "revenue", "sales", "sold", "unit", "quantity", "transaction", "order",
    "price", "value",
)

# Any one of these (plus a year, "top N", or a recognized dimension keyword) turns a
# metric mention into a complete, answerable question - "total revenue" and "how many
# transactions were there?" both qualify and are answered immediately with an all-time
# default, matching current behavior; bare "revenue" / "show me revenue" do not.
_QUALIFYING_WORDS = ("total", "average", "avg", "by", "how many", "all")

GREETING_REPLY = (
    "Hi! \U0001F44B How can I help you today? You can ask me about revenue, sales, "
    "products, stores, payments, suppliers, or business trends."
)

# Deliberately does NOT mention customers: the semantic layer excludes customer data
# entirely (see db/README.md - PII exclusion), so claiming it here would set the user
# up for a query that can never succeed.
HELP_REPLY = (
    "You can ask me about:\n"
    "- Revenue and sales\n"
    "- Units sold and transaction counts\n"
    "- Stores, divisions, and districts\n"
    "- Products and suppliers\n"
    "- Manufacturer countries\n"
    "- Payment types and banks\n"
    "- Time-based trends (by year, quarter, or month)\n"
    "- Top-performing items, suppliers, or divisions\n\n"
    'For example: "total revenue by division", "top 10 items by revenue", '
    '"average order value", "revenue by quarter in 2020".'
)

CLARIFYING_REPLY_TIME_PERIOD = (
    "Sure - which time period would you like to look at? For example, a specific "
    "year (e.g. 2020), or all-time?"
)

# For questions with no recognizable metric, dimension, year, or "top N" at all -
# nothing in the schema to anchor a query to. Deliberately states the actual scope
# rather than guessing: silently defaulting to a bare revenue total for an unrelated
# question (e.g. "list the sales reps in the company" - no such data exists) is exactly
# the "lacking interaction" failure mode this replaces.
CLARIFYING_REPLY_SCOPE = (
    "I couldn't find a way to answer that from the sales data I have - revenue, units "
    "sold, transactions, by division, district, item, supplier, manufacturer country, "
    "payment type or bank, over time. Could you rephrase, or ask about one of those?"
)


def _classify_query_shape(q: str) -> tuple[bool, bool, bool]:
    """Returns (has_metric, has_dimension, has_qualifier). A dimension keyword or a
    qualifier (total/average/"by"/a year/"top N") is, on its own, enough signal to
    treat the question as answerable - a metric word with neither is what makes a
    question a fragment ("revenue") rather than a real question ("total revenue")."""
    has_metric = any(_word_in(w, q) for w in _METRIC_SIGNAL_WORDS)
    has_dimension = any(_word_in(kw, q) for kw, _ in _DIMENSION_KEYWORDS)
    has_qualifier = (
        any(_word_in(w, q) for w in _QUALIFYING_WORDS)
        or _YEAR_RE.search(q) is not None
        or _TOP_N_RE.search(q) is not None
    )
    return has_metric, has_dimension, has_qualifier


def _is_listing_intent(q: str) -> bool:
    """"What are the available X" / "list the X" - wants distinct values, not a metric
    ranking. Excludes "top N" phrasing, which is a real ranking request even though it
    might also contain "available" incidentally.

    A bare "list " prefix alone is NOT enough - "List the name of the sales in the
    company" starts with "list " but isn't a dimension-listing request at all (there's
    no recognizable dimension in it); treating it as one produced a nonsense product
    list instead of correctly falling through to a clarification. "list " only counts
    when paired with an actual recognized dimension keyword; "available" is a strong
    enough signal on its own (see _build_listing_sql's item_name default for the bare
    "what's available?" case).

    A metric word (revenue/sales/units/...) rules listing out entirely, regardless of
    the above: "List total revenue... broken down by district" starts with "list " and
    contains the dimension keyword "district", but it's a breakdown request ("list" used
    as a synonym for "show me"), not an enumeration of distinct district names - "list"
    is not always the enumeration verb."""
    has_metric, _, _ = _classify_query_shape(q)
    if has_metric:
        return False
    if _TOP_N_RE.search(q) or _word_in("top", q):
        return False
    if any(_word_in(w, q) for w in _LISTING_TRIGGER_WORDS):
        return True
    if q.strip().startswith("list "):
        return any(_word_in(kw, q) for kw, _ in _DIMENSION_KEYWORDS)
    return False


# Leading filler words a bare follow-up filter ("Only for 2024") is stripped of before
# checking whether what's left is just a year. "+" (not "?") because a phrase like
# "Only for 2024" has two filler words in a row - stripping just one would leave "for
# 2024", which then fails the bare-year check.
_FOLLOWUP_FILLER_RE = re.compile(
    r"^\s*(?:(?:only|just|and|what about|for|in)\s+)+", re.IGNORECASE
)
_BARE_YEAR_RE = re.compile(r"^\s*(19|20)\d{2}\s*\??\s*$")

# Matches exactly the SQL shape this class's own generate_sql() produces - used to
# rebuild a previous turn's query with a new year filter for follow-up merging. Deliberately
# tied to this class's own output format, not a general SQL parser.
_SQL_SHAPE_RE = re.compile(
    r"^SELECT (?P<select>.+?) FROM (?P<from>\S+)"
    r"(?: WHERE (?P<where>.+?))?"
    r"(?: GROUP BY (?P<groupby>.+?))?"
    r"(?: ORDER BY (?P<orderby>.+?))?"
    r"(?: LIMIT (?P<limit>\d+))?$"
)

# "'Dhaka'", "'Pepsi - 12 oz cans'", or double-quoted equivalents - a quoted phrase in
# the question is a strong signal the user named a specific value, not a dimension to
# group by.
_QUOTED_VALUE_RE = re.compile(r"'([^']+)'|\"([^\"]+)\"")


def _extract_quoted_values(question: str) -> list[str]:
    return [m.group(1) or m.group(2) for m in _QUOTED_VALUE_RE.finditer(question)]


def _sql_escape(value: str) -> str:
    """Defense in depth beyond the AST guardrail: this is direct string interpolation
    of question-derived (user-influenced) text into SQL text, so it must be escaped at
    construction time, not just relied on to be caught downstream."""
    return value.replace("'", "''")


@dataclass(frozen=True)
class _NamedFilter:
    column: str
    op: str  # "=" | "IN" | "ILIKE"
    values: tuple[str, ...]  # one value for "="/"ILIKE" with a single match, more for "IN"/multi-ILIKE

    def to_sql(self) -> str:
        if self.op == "=":
            return f"{self.column} = '{_sql_escape(self.values[0])}'"
        if self.op == "IN":
            joined = ", ".join(f"'{_sql_escape(v)}'" for v in self.values)
            return f"{self.column} IN ({joined})"
        if self.op == "ILIKE":
            if len(self.values) == 1:
                return f"{self.column} ILIKE '%{_sql_escape(self.values[0])}%'"
            clauses = " OR ".join(f"{self.column} ILIKE '%{_sql_escape(v)}%'" for v in self.values)
            return f"({clauses})"
        raise ValueError(f"unknown filter op {self.op!r}")  # pragma: no cover - internal invariant


def _extract_named_filters(
    question: str, catalog: SchemaCatalog
) -> tuple[list[_NamedFilter], set[str], str | None]:
    """Resolves named entities in the question ("'Dhaka'", "cash", "'China'") into
    WHERE filters against the real enum values loaded from the database
    (catalog.enum_hints) - not a hardcoded list, so it can't drift from the real data
    the way the original spec's hardcoded hints did (see db/README.md).

    Matching is case-insensitive and works whether the value is quoted ("'Dhaka'") or
    bare ("cash transactions") - `_word_in` against each known enum value, lowercased.
    The *canonical* (as-stored) value is what goes into the generated SQL, so this also
    transparently handles the known man_country casing quirk ("poland" stored
    lowercase): asking about "'Poland'" still filters on the value that's actually in
    the database.

    Returns (filters, consumed_dimensions, compare_dimension):
      - consumed_dimensions: dimension columns resolved as a *filter* value, not a
        group-by request - "for the 'Dhaka' division" filters to Dhaka, it doesn't ask
        to break down by division, so `store_division` must not also be picked as the
        GROUP BY target by the normal keyword scan.
      - compare_dimension: set when 2+ distinct values matched the same dimension
        ("compare 'Dhaka' and 'Chittagong'") - that dimension is forced as the GROUP BY
        target instead of being excluded, since a compare request specifically wants
        one row per named entity.
    """
    q = question.lower()
    filters: list[_NamedFilter] = []
    consumed_dimensions: set[str] = set()
    compare_dimension: str | None = None
    matched_lower: set[str] = set()

    for column, values in catalog.enum_hints.items():
        matches = [v for v in values if v and _word_in(v.lower(), q)]
        if not matches:
            continue
        matched_lower.update(v.lower() for v in matches)
        if len(matches) >= 2:
            filters.append(_NamedFilter(column, "IN", tuple(matches)))
            compare_dimension = column
        else:
            filters.append(_NamedFilter(column, "=", (matches[0],)))
        consumed_dimensions.add(column)

    # Quoted phrases not already resolved as a known enum value are treated as
    # item_name references (free text, not enum-hinted - exact match is too brittle
    # for product names, hence ILIKE rather than "=").
    unmatched_quoted = [
        v for v in _extract_quoted_values(question) if v.lower() not in matched_lower
    ]
    if unmatched_quoted:
        filters.append(_NamedFilter("item_name", "ILIKE", tuple(unmatched_quoted)))
        if len(unmatched_quoted) >= 2 and compare_dimension is None:
            compare_dimension = "item_name"

    return filters, consumed_dimensions, compare_dimension


class MockLLMProvider(LLMProvider):
    """Deterministic rule-based router: extracts a metric, an optional group-by dimension,
    an optional year filter, and an optional "top N" clause from the question, then builds
    SQL against mv_sales_daily_rollup (default) or mv_sales_analysis (when the question
    needs a Tier-2-only dimension). No network calls, no randomness - same question always
    produces the same SQL, which is what makes it usable for eval harness ground-truth
    comparisons.
    """

    async def classify(self, question: str, history: list[ConversationTurn]) -> IntentResult:
        q = question.lower().strip()

        if any(_word_in(w, q) for w in _GREETING_WORDS):
            return IntentResult(intent=Intent.GREETING, reply=GREETING_REPLY)

        if any(_word_in(w, q) for w in _HELP_PHRASES):
            return IntentResult(intent=Intent.HELP, reply=HELP_REPLY)

        # Only fires when there's an actual previous result to replay - otherwise falls
        # through to the normal shape-based rules below (which will most likely ask for
        # clarification, since "explain"/"summarize" carry no metric/dimension signal).
        if any(_word_in(p, q) for p in _EXPLAIN_PREVIOUS_PHRASES) and any(
            t.narrative_text for t in history
        ):
            return IntentResult(intent=Intent.EXPLAIN_PREVIOUS)

        if any(_word_in(p, q) for p in _DATA_COVERAGE_PHRASES):
            return IntentResult(intent=Intent.DATA_COVERAGE)

        # Forced QUERY regardless of the shape rules below: a listing question with no
        # dimension named ("what's available?") should still attempt an answer (default
        # dimension, see _build_listing_sql), not get bucketed into the scope-limitation
        # clarification the shape rules would otherwise give a signal-less question.
        if _is_listing_intent(q):
            return IntentResult(intent=Intent.QUERY)

        has_metric, has_dimension, has_qualifier = _classify_query_shape(q)

        if has_dimension or has_qualifier:
            # A qualifier alone (e.g. just a year) is enough - this is also what keeps
            # the bare-filter follow-up case ("Only for 2024") routed to QUERY, since
            # it has neither a metric nor a dimension keyword, only the year.
            return IntentResult(intent=Intent.QUERY)

        if has_metric:
            return IntentResult(
                intent=Intent.CLARIFICATION_NEEDED,
                clarifying_question=CLARIFYING_REPLY_TIME_PERIOD,
            )

        # Nothing recognizable at all - no metric, no dimension, no year/top-N. Stating
        # the actual scope beats silently guessing a generic default.
        return IntentResult(
            intent=Intent.CLARIFICATION_NEEDED, clarifying_question=CLARIFYING_REPLY_SCOPE
        )

    @staticmethod
    def _try_merge_with_previous(question: str, history: list[ConversationTurn]) -> str | None:
        """Handles the bare-filter follow-up case ("Only for 2024" after "Show revenue
        by year"): if the question strips down to just a year, rebuild the most recent
        query in `history` that actually ran (skipping conversational turns, which have
        `sql=None`) with that year as the new filter, keeping its dimension/metric/order/
        limit unchanged. Returns None (falls through to normal generation) for anything
        that isn't this exact bare-filter shape."""
        stripped = _FOLLOWUP_FILLER_RE.sub("", question.strip())
        if not _BARE_YEAR_RE.match(stripped):
            return None

        prev_sql = next((t.sql for t in reversed(history) if t.sql), None)
        if not prev_sql:
            return None

        match = _SQL_SHAPE_RE.match(prev_sql.strip())
        if not match:
            return None

        year = int(_YEAR_RE.search(stripped).group())
        parts = [f"SELECT {match.group('select')}", f"FROM {match.group('from')}"]
        parts.append(f"WHERE sale_year = {year}")
        if match.group("groupby"):
            parts.append(f"GROUP BY {match.group('groupby')}")
        if match.group("orderby"):
            parts.append(f"ORDER BY {match.group('orderby')}")
        if match.group("limit"):
            parts.append(f"LIMIT {match.group('limit')}")
        return " ".join(parts)

    @staticmethod
    def _build_listing_sql(q: str) -> str:
        """"What are the available products/suppliers/divisions" - distinct values, not
        a metric ranking. Defaults to item_name (the most intuitive reading of a bare
        "what's available?") when no dimension is named."""
        dimension = next(
            (col for kw, col in _DIMENSION_KEYWORDS if _word_in(kw, q)), "item_name"
        )
        use_tier2 = dimension in _TIER2_ONLY_DIMENSIONS
        table = "public.mv_sales_analysis" if use_tier2 else "public.mv_sales_daily_rollup"
        return f"SELECT DISTINCT {dimension} FROM {table} ORDER BY {dimension} LIMIT 500"

    async def generate_sql(
        self,
        question: str,
        catalog: SchemaCatalog,
        history: list[ConversationTurn],
        error_feedback: str | None = None,
    ) -> str:
        merged = self._try_merge_with_previous(question, history)
        if merged is not None:
            return merged

        q = question.lower()

        if _is_listing_intent(q):
            return self._build_listing_sql(q)

        filters, consumed_dimensions, compare_dimension = _extract_named_filters(question, catalog)

        if compare_dimension:
            # A compare request ("compare 'Dhaka' and 'Chittagong'") explicitly wants
            # one row per named entity - the named dimension IS the group-by target,
            # overriding the normal scan entirely.
            dimension = compare_dimension
        else:
            # Skip any dimension already resolved as a filter value: "for the 'Dhaka'
            # division" filters to Dhaka, it doesn't also ask to group by division.
            dimension = next(
                (
                    col
                    for kw, col in _DIMENSION_KEYWORDS
                    if _word_in(kw, q) and col not in consumed_dimensions
                ),
                None,
            )

        # AVG must always run against Tier 2 (one row per transaction), never Tier 1: the
        # rollup table is pre-aggregated by date x division x district x item, so
        # AVG(total_revenue) over it would average already-summed group totals - a
        # different, less meaningful number than a true average order value.
        is_average = _word_in("average", q) or _word_in("avg", q)
        use_tier2 = (
            is_average
            or dimension in _TIER2_ONLY_DIMENSIONS
            or any(f.column in _TIER2_ONLY_DIMENSIONS for f in filters)
        )
        table = "public.mv_sales_analysis" if use_tier2 else "public.mv_sales_daily_rollup"

        metric_expr, metric_alias = self._metric(q, use_tier2)

        where_parts: list[str] = []
        year_match = _YEAR_RE.search(q)
        if year_match:
            where_parts.append(f"sale_year = {int(year_match.group())}")
        where_parts.extend(f.to_sql() for f in filters)
        where_clause = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""

        top_n_match = _TOP_N_RE.search(q)
        limit_n = int(top_n_match.group(1)) if top_n_match else None

        if dimension:
            select_cols = f"{dimension}, {metric_expr} AS {metric_alias}"
            group_by = f"GROUP BY {dimension}"
            order_by = f"ORDER BY {metric_alias} DESC"
        else:
            select_cols = f"{metric_expr} AS {metric_alias}"
            group_by = ""
            order_by = ""

        limit_clause = f"LIMIT {limit_n}" if limit_n else ""

        parts = [f"SELECT {select_cols}", f"FROM {table}"]
        if where_clause:
            parts.append(where_clause)
        if group_by:
            parts.append(group_by)
        if order_by:
            parts.append(order_by)
        if limit_clause:
            parts.append(limit_clause)
        return " ".join(parts)

    @staticmethod
    def _metric(q: str, use_tier2: bool) -> tuple[str, str]:
        # Checked first and deliberately: "average" must win over a coincidental
        # whole-word match like "order" inside "average order value" - keyword order
        # here is the actual precedence, not just a list of options.
        if _word_in("average", q) or _word_in("avg", q):
            return ("AVG(total_price)" if use_tier2 else "AVG(total_revenue)", "avg_revenue")
        # Explicit "revenue" is unambiguous and must win over a unit/transaction word
        # appearing elsewhere in the same sentence as incidental filter language, not
        # the actual requested metric - e.g. "total revenue... for cash transactions"
        # asks for revenue; "transactions" there describes the filter, not the metric.
        # ("sales" is NOT given this priority - unlike "revenue" it's genuinely
        # ambiguous between revenue and volume, e.g. "total sales volume".)
        if _word_in("revenue", q):
            return ("SUM(total_price)" if use_tier2 else "SUM(total_revenue)", "total_revenue")
        if any(_word_in(kw, q) for kw in ("unit", "quantity", "volume", "sold")):
            return ("SUM(quantity)" if use_tier2 else "SUM(total_units_sold)", "total_units")
        if any(_word_in(kw, q) for kw in ("transaction", "order", "how many", "count")):
            return ("COUNT(*)" if use_tier2 else "SUM(transaction_count)", "transaction_count")
        # default: revenue/sales
        return ("SUM(total_price)" if use_tier2 else "SUM(total_revenue)", "total_revenue")
