import pytest

from app.security.ast_guardrail import GuardrailViolation, validate_and_reserialize_sql

ALLOWED = [
    pytest.param(
        "SELECT store_division, SUM(total_revenue) AS rev "
        "FROM public.mv_sales_daily_rollup GROUP BY 1",
        id="simple-aggregate",
    ),
    pytest.param(
        "SELECT * FROM mv_sales_analysis WHERE sale_year = 2020",
        id="unqualified-table-name-defaults-to-public",
    ),
    pytest.param(
        "SELECT CAST(total_price AS int) FROM public.mv_sales_analysis",
        id="allow-listed-function-cast",
    ),
    pytest.param(
        "SELECT COALESCE(payment_bank, 'unknown') FROM public.mv_sales_analysis",
        id="allow-listed-function-coalesce",
    ),
    pytest.param(
        "SELECT store_division FROM public.mv_sales_analysis "
        "UNION SELECT store_division FROM public.mv_sales_daily_rollup",
        id="union-of-two-allowed-tables",
    ),
    pytest.param(
        "SELECT a.store_division, b.total_units_sold FROM public.mv_sales_analysis a "
        "JOIN public.mv_sales_daily_rollup b ON a.store_division = b.store_division",
        id="join-of-two-allowed-tables",
    ),
    pytest.param(
        "SELECT 'a; DROP TABLE x' AS note FROM public.mv_sales_analysis",
        id="semicolon-inside-string-literal-is-not-a-statement-separator",
    ),
    pytest.param(
        "SELECT store_division, SUM(total_revenue) AS rev FROM public.mv_sales_daily_rollup "
        "WHERE sale_year = 2019 AND store_division = 'DHAKA' GROUP BY store_division",
        id="compound-where-with-and",
    ),
    pytest.param(
        "SELECT store_division FROM public.mv_sales_analysis "
        "WHERE store_division = 'DHAKA' OR store_division = 'SYLHET'",
        id="compound-where-with-or",
    ),
    pytest.param(
        "SELECT store_division, SUM(total_revenue) AS rev FROM public.mv_sales_daily_rollup "
        "WHERE store_division IN ('DHAKA', 'CHITTAGONG') GROUP BY store_division",
        id="in-clause-with-multiple-values",
    ),
    pytest.param(
        "SELECT item_name FROM public.mv_sales_analysis WHERE item_name ILIKE '%pepsi%'",
        id="ilike-pattern-match",
    ),
]


@pytest.mark.parametrize("sql", ALLOWED)
def test_allows_legitimate_queries(sql):
    result = validate_and_reserialize_sql(sql)
    assert "LIMIT" in result.upper()


def test_adds_limit_when_missing():
    result = validate_and_reserialize_sql("SELECT * FROM public.mv_sales_analysis")
    assert result.rstrip().upper().endswith("LIMIT 500")


def test_caps_limit_that_exceeds_max():
    result = validate_and_reserialize_sql(
        "SELECT * FROM public.mv_sales_analysis LIMIT 100000"
    )
    assert result.rstrip().upper().endswith("LIMIT 500")


def test_preserves_limit_within_max():
    result = validate_and_reserialize_sql("SELECT * FROM public.mv_sales_analysis LIMIT 10")
    assert result.rstrip().upper().endswith("LIMIT 10")


class TestRejectsStackedStatements:
    def test_two_selects(self):
        with pytest.raises(GuardrailViolation, match="one SQL statement"):
            validate_and_reserialize_sql("SELECT 1; SELECT 2")

    def test_select_then_drop(self):
        with pytest.raises(GuardrailViolation, match="one SQL statement"):
            validate_and_reserialize_sql(
                "SELECT 1; DROP TABLE public.mv_sales_analysis"
            )


class TestRejectsForbiddenTopLevelStatements:
    @pytest.mark.parametrize(
        "sql",
        [
            "INSERT INTO public.mv_sales_analysis (fact_key) VALUES (1)",
            "UPDATE public.mv_sales_analysis SET total_price = 0",
            "DELETE FROM public.mv_sales_analysis",
            "DROP TABLE public.mv_sales_analysis",
            "ALTER TABLE public.mv_sales_analysis ADD COLUMN x int",
            "CREATE TABLE evil (x int)",
            "COPY public.mv_sales_analysis TO STDOUT",
            "SET statement_timeout = 0",
            "GRANT SELECT ON public.mv_sales_analysis TO PUBLIC",
        ],
    )
    def test_rejected(self, sql):
        with pytest.raises(GuardrailViolation):
            validate_and_reserialize_sql(sql)


class TestRejectsCteWrappedDml:
    """The exact attack the spec calls out by name: DML hidden inside a CTE of an
    otherwise SELECT-shaped statement. Top-level shape check alone would miss this -
    only the full-tree walk catches it."""

    def test_cte_wrapped_delete(self):
        with pytest.raises(GuardrailViolation, match="Delete"):
            validate_and_reserialize_sql(
                "WITH x AS (DELETE FROM public.mv_sales_analysis RETURNING *) "
                "SELECT * FROM x"
            )

    def test_cte_wrapped_update(self):
        with pytest.raises(GuardrailViolation, match="Update"):
            validate_and_reserialize_sql(
                "WITH x AS (UPDATE public.mv_sales_analysis SET total_price = 0 RETURNING *) "
                "SELECT * FROM x"
            )

    def test_cte_wrapped_insert(self):
        with pytest.raises(GuardrailViolation, match="Insert"):
            validate_and_reserialize_sql(
                "WITH x AS (INSERT INTO public.mv_sales_analysis (fact_key) VALUES (1) RETURNING *) "
                "SELECT * FROM x"
            )


class TestTableAllowList:
    def test_rejects_core_schema(self):
        with pytest.raises(GuardrailViolation, match="core.fact_table"):
            validate_and_reserialize_sql("SELECT * FROM core.fact_table")

    def test_rejects_core_schema_pii_table(self):
        with pytest.raises(GuardrailViolation, match="core.customer_dim"):
            validate_and_reserialize_sql("SELECT * FROM core.customer_dim")

    def test_rejects_same_table_name_in_wrong_schema(self):
        """A table named identically to an allowed one, but in a different schema,
        must still be rejected - the allow-list is (schema, name), not name alone."""
        with pytest.raises(GuardrailViolation):
            validate_and_reserialize_sql("SELECT * FROM core.mv_sales_analysis")

    def test_rejects_system_catalog(self):
        with pytest.raises(GuardrailViolation):
            validate_and_reserialize_sql("SELECT * FROM pg_catalog.pg_shadow")


class TestFunctionAllowList:
    """Regression tests for the spec's original bug: sqlglot represents known SQL
    functions (SUM, CAST, ...) as dedicated exp.* classes, not exp.Anonymous. A check
    that only inspects exp.Anonymous never enforces the allow-list against any
    function sqlglot recognizes - only against ones it doesn't."""

    def test_rejects_unrecognized_anonymous_function(self):
        with pytest.raises(GuardrailViolation, match="SOME_FUNC"):
            validate_and_reserialize_sql(
                "SELECT some_func(total_price) FROM public.mv_sales_analysis"
            )

    def test_rejects_dangerous_anonymous_function(self):
        with pytest.raises(GuardrailViolation, match="PG_SLEEP"):
            validate_and_reserialize_sql(
                "SELECT pg_sleep(10) FROM public.mv_sales_analysis"
            )

    def test_rejects_recognized_but_non_allow_listed_function(self):
        """EXTRACT is a real, sqlglot-recognized function (exp.Extract, not
        exp.Anonymous) - the naive exp.Anonymous-only check would have let this
        through silently. It must still be rejected because it's not on the
        allow-list."""
        with pytest.raises(GuardrailViolation, match="EXTRACT"):
            validate_and_reserialize_sql(
                "SELECT EXTRACT(YEAR FROM sale_date) FROM public.mv_sales_analysis"
            )

    @pytest.mark.parametrize(
        "func_call",
        ["SUM(total_price)", "AVG(unit_price)", "COUNT(*)", "MIN(total_price)",
         "MAX(total_price)", "ROUND(total_price, 2)"],
    )
    def test_allows_listed_aggregate_functions(self, func_call):
        validate_and_reserialize_sql(f"SELECT {func_call} FROM public.mv_sales_analysis")


class TestBooleanConnectorsAreNotFunctions:
    """Regression tests for a guardrail bug found while adding compound-WHERE support:
    sqlglot represents AND/OR/XOR (exp.Connector, a subclass of exp.Func) as function
    nodes internally. A naive `isinstance(node, exp.Func)` check - exactly the pattern
    this guardrail otherwise correctly uses for real functions - was rejecting every
    two-condition WHERE clause with "Function 'AND' is not in the allow-list", since no
    query before this had ever combined two WHERE conditions. exp.Connector must be
    excluded from the function-allowlist check without weakening it for anything else -
    see test_still_rejects_dangerous_function_nested_inside_or below."""

    def test_allows_and_in_where_clause(self):
        validate_and_reserialize_sql(
            "SELECT store_division FROM public.mv_sales_analysis "
            "WHERE sale_year = 2020 AND store_division = 'DHAKA'"
        )

    def test_allows_or_in_where_clause(self):
        validate_and_reserialize_sql(
            "SELECT store_division FROM public.mv_sales_analysis "
            "WHERE store_division = 'DHAKA' OR store_division = 'SYLHET'"
        )

    def test_still_rejects_dangerous_function_nested_inside_or(self):
        """The Connector exemption must not become a loophole - a forbidden function
        nested inside an OR/AND is still walked into and still rejected."""
        with pytest.raises(GuardrailViolation, match="PG_SLEEP"):
            validate_and_reserialize_sql(
                "SELECT 1 FROM public.mv_sales_analysis "
                "WHERE sale_year = 2020 OR pg_sleep(1) IS NOT NULL"
            )


def test_reserializes_rather_than_returning_original_text():
    """The guardrail must never hand back the caller's original string verbatim -
    only the re-serialized AST, so what gets executed is provably what was validated."""
    raw = "select   store_division ,SUM(total_revenue)  from public.mv_sales_daily_rollup group by 1"
    result = validate_and_reserialize_sql(raw)
    assert result != raw
    assert result.startswith("SELECT")
