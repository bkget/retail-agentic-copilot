"""End-to-end tests against the real semantic layer (see conftest.py) using
MockLLMProvider - proves the full pipeline (classify_input -> map_terms_to_columns ->
session-state resolution -> generate_sql -> guardrail -> run_sql -> narrative/
visualization -> update_session) works without needing a Gemini key.

Multi-turn tests reuse a single session_id across sequential `orchestrator.answer(...)`
calls and let the Orchestrator's own SessionStore carry state between them, rather than
hand-building a history list - that's the real code path a live client exercises.
"""

from __future__ import annotations

import pytest

from app.agent.llm_provider import MockLLMProvider
from app.agent.orchestrator import Orchestrator, OrchestratorError
from app.schema.catalog import load_catalog
from app.session.store import SessionStore


@pytest.fixture
async def catalog(agent_pool):
    return await load_catalog(agent_pool)


@pytest.fixture
def orchestrator(agent_pool):
    session_store = SessionStore(ttl_seconds=1800, max_turns=10)
    return Orchestrator(MockLLMProvider(), agent_pool, session_store)


async def test_revenue_by_division(orchestrator, catalog):
    result = await orchestrator.answer("What is total revenue by division?", catalog, "s")
    assert result.response_type == "query"
    assert result.row_count > 0
    assert "mv_sales_daily_rollup" in result.sql_executed
    assert result.narrative_text
    assert result.visualization.render_chart is True
    assert result.metrics["total_ms"] >= 0


async def test_revenue_by_division_filtered_by_year(orchestrator, catalog):
    result = await orchestrator.answer("Total revenue by division in 2019", catalog, "s")
    assert "sale_year = 2019" in result.sql_executed
    assert result.row_count > 0


async def test_top_n_items_uses_tier2_for_supplier(orchestrator, catalog):
    result = await orchestrator.answer("Top 5 suppliers by revenue", catalog, "s")
    assert "mv_sales_analysis" in result.sql_executed
    assert result.row_count <= 5


async def test_total_transactions_no_dimension(orchestrator, catalog):
    """Complete question with no time period ("were there" implies all-time) - must
    still answer immediately with a sensible default, not ask for clarification."""
    result = await orchestrator.answer("How many transactions were there?", catalog, "s")
    assert result.response_type == "query"
    assert result.row_count == 1
    assert result.visualization.render_chart is False


async def test_guardrail_actually_runs_in_the_loop(orchestrator, catalog):
    """Sanity check that the guardrail is genuinely wired into the loop, not bypassed:
    every query the mock generates must come back with a LIMIT clause and reference only
    the two allowed public views."""
    result = await orchestrator.answer("Revenue by payment type", catalog, "s")
    sql = result.sql_executed.lower()
    assert "limit" in sql
    assert "core." not in sql


async def test_average_keyword_wins_over_incidental_order_substring(orchestrator, catalog):
    """Regression test: 'average order value' was routing to COUNT because 'order' is
    a substring of the question and was checked before 'average'."""
    result = await orchestrator.answer("What is the average order value?", catalog, "s")
    assert "AVG(" in result.sql_executed
    assert "COUNT(" not in result.sql_executed
    assert "$" in result.narrative_text  # it's a monetary average, currency label is correct here


async def test_average_by_dimension_uses_tier2_not_pre_aggregated_rollup(orchestrator, catalog):
    """Regression test: AVG(total_revenue) over the Tier-1 rollup would average
    already-summed per-group totals, not real transaction values - a true average must
    run against the row-level Tier 2 view."""
    result = await orchestrator.answer("Average order value by division", catalog, "s")
    assert "mv_sales_analysis" in result.sql_executed
    assert "AVG(total_price)" in result.sql_executed


async def test_unanswerable_question_does_not_crash(orchestrator, catalog):
    # A nonsense/unrecognized string with no metric, dimension, or reference signal at
    # all classifies as UNKNOWN (not CLARIFICATION) per the taxonomy - it explains
    # capabilities and asks the user to rephrase (response_type "conversational"),
    # rather than asking one targeted question about something it partially
    # recognized. It must not crash, and must not silently run a default query either.
    result = await orchestrator.answer("asdkfjaslkdfj", catalog, "s")
    assert result.response_type == "conversational"
    assert result.sql_executed is None


class TestConversationalIntent:
    """Regression tests for the screenshot bug: every message, including greetings and
    'what can I ask' style questions, was falling through to a default revenue query."""

    @pytest.mark.parametrize(
        "message", ["Hi", "Hello", "Hey", "Good morning", "Thanks", "Good afternoon!", "Hii"]
    )
    async def test_greeting_never_hits_the_database(self, orchestrator, catalog, message):
        result = await orchestrator.answer(message, catalog, "s")
        assert result.response_type == "conversational"
        assert result.sql_executed is None
        assert result.row_count is None
        assert result.narrative_text

    @pytest.mark.parametrize(
        "message",
        [
            "what can you help me with?",
            "what kind of questions can I ask?",
            "tell me about yourself",
            "what columns are available to ask here?",
            # Regression case: non-adjacent phrasing ("are the" in between) doesn't
            # contain the literal substring "what columns" - this exact question fell
            # through to the default revenue query before "columns" was added as its
            # own standalone trigger word.
            "What are the columns which are available to ask here?",
            # Regression case (third PDF review round): fell all the way through to the
            # generic "I couldn't find a way to answer that" scope-clarification
            # message instead of actually answering, since it matched neither the
            # original HELP phrases nor the DATA_COVERAGE ones (which are specifically
            # about the year range, not domains).
            "what kind of data you've access to?",
        ],
    )
    async def test_schema_info_request_never_hits_the_database(self, orchestrator, catalog, message):
        result = await orchestrator.answer(message, catalog, "s")
        assert result.response_type == "conversational"
        assert result.sql_executed is None
        # Must reflect real, queryable columns - not the generic example domain list.
        assert "revenue" in result.narrative_text.lower()
        assert "customer" not in result.narrative_text.lower()

    @pytest.mark.parametrize("message", ["revenue", "Show me revenue", "sales"])
    async def test_vague_fragment_asks_for_clarification(self, orchestrator, catalog, message):
        result = await orchestrator.answer(message, catalog, "s")
        assert result.response_type == "clarification"
        assert result.sql_executed is None
        assert "?" in result.narrative_text

    @pytest.mark.parametrize(
        "message", ["total revenue", "how many transactions were there?", "average order value"]
    )
    async def test_complete_question_still_answers_immediately(self, orchestrator, catalog, message):
        """Scoping decision: only genuinely vague fragments ask for clarification -
        complete questions with a qualifying word keep working exactly as before."""
        result = await orchestrator.answer(message, catalog, "s")
        assert result.response_type == "query"

    async def test_followup_bare_year_refines_previous_query(self, orchestrator, catalog):
        first = await orchestrator.answer("Show revenue by year", catalog, "s1")
        assert first.response_type == "query"
        assert "sale_year" in first.sql_executed

        second = await orchestrator.answer("Only for 2024", catalog, "s1")
        assert second.response_type == "query"
        assert "sale_year = 2024" in second.sql_executed
        # Must inherit the year grouping from turn 1, not fall back to an unrelated
        # ungrouped default aggregate.
        assert "GROUP BY sale_year" in second.sql_executed

    async def test_followup_without_prior_history_asks_instead_of_guessing(self, orchestrator, catalog):
        """A bare "only for 2024" with no prior turn in this session has no metric to
        inherit and none of its own - correctly asks what to measure, rather than the
        old, less strict behavior of silently defaulting to an arbitrary metric."""
        result = await orchestrator.answer("Only for 2024", catalog, "fresh-1")
        assert result.response_type == "clarification"
        assert result.sql_executed is None


class TestPdfReviewFindings:
    """Regression tests for the second round of gaps found via a review of 5 real
    sample interactions (see the project plan). Each of these previously either
    silently returned a confidently wrong number or answered a different question than
    the one asked."""

    async def test_unanswerable_long_question_asks_for_clarification(self, orchestrator, catalog):
        """"List the name of the sales in the company" - no salesperson data exists in
        this schema. "sales" is a recognized (if ambiguous) metric signal word, so this
        is a metric fragment with nothing else qualifying it - CLARIFICATION, not a
        silently-run default revenue query."""
        result = await orchestrator.answer(
            "List the name of the sales in the company", catalog, "s"
        )
        assert result.response_type == "clarification"
        assert result.sql_executed is None

    async def test_data_coverage_question_answered_directly(self, orchestrator, catalog):
        """"Which year data you have access to?" must state the actual year range, not
        get routed through the analytical pipeline as a "revenue by year" query."""
        result = await orchestrator.answer("Which year data you have access to?", catalog, "s")
        assert result.response_type == "conversational"
        assert result.sql_executed is None
        assert str(catalog.year_min) in result.narrative_text
        assert str(catalog.year_max) in result.narrative_text

    async def test_data_coverage_phrasing_does_not_hijack_real_ranking_question(
        self, orchestrator, catalog
    ):
        """"What year had the highest revenue" is a real analytical question (find the
        top year), not a meta-question about data coverage - the coverage-phrase
        detection must not be so broad it swallows this."""
        result = await orchestrator.answer("What year had the highest revenue?", catalog, "s")
        assert result.response_type == "query"

    async def test_listing_products_returns_actual_names_not_a_ranking(self, orchestrator, catalog):
        """"What are the available products in our store?" must list product names, not
        rank them by revenue - the two are different questions."""
        result = await orchestrator.answer(
            "What are the available products in our store?", catalog, "s"
        )
        assert result.response_type == "query"
        assert "DISTINCT item_name" in result.sql_executed
        assert "SUM(" not in result.sql_executed
        assert "distinct" in result.narrative_text.lower()

    async def test_listing_does_not_hijack_unrelated_list_request(self, orchestrator, catalog):
        """A bare "list " prefix alone isn't enough signal - only "list " + an actual
        recognized dimension, or "available", counts. This question also has no
        recognized dimension of its own, so it's the same CLARIFICATION case as
        test_unanswerable_long_question_asks_for_clarification, above - it must not
        get treated as a listing request, and must not run any SQL at all."""
        result = await orchestrator.answer(
            "List the name of the sales in the company", catalog, "s"
        )
        assert result.sql_executed is None

    async def test_listing_ranking_still_ranks_when_top_n_present(self, orchestrator, catalog):
        """"Top 5 available products" should still rank by revenue - "available" alone
        doesn't override an explicit ranking request."""
        result = await orchestrator.answer("Top 5 available products by revenue", catalog, "s")
        assert "DISTINCT" not in result.sql_executed
        assert "SUM(" in result.sql_executed

    async def test_reference_to_previous_reruns_the_inherited_query(self, orchestrator, catalog):
        """"Explain the above result in plain english" isn't a separate top-level
        intent in the new taxonomy - EXPLAIN_PREVIOUS's old replay capability is now
        reference resolution inside DATABASE_QUERY, so this re-resolves via inherited
        session state (same metric/dimension as turn 1) and re-answers as a query,
        rather than replaying cached text verbatim."""
        first = await orchestrator.answer("Revenue by item name", catalog, "s2")
        assert first.response_type == "query"
        assert first.narrative_text

        second = await orchestrator.answer(
            "Explain the above result in plain english", catalog, "s2"
        )
        assert second.response_type == "query"
        assert "item_name" in second.sql_executed
        assert "total_revenue" in second.sql_executed.lower() or "SUM(" in second.sql_executed

    async def test_reference_without_prior_history_asks_instead_of_guessing(
        self, orchestrator, catalog
    ):
        """No crash, and no guess, when there's nothing in this session to refer to
        yet."""
        result = await orchestrator.answer("Explain the above result", catalog, "fresh-2")
        assert result.response_type == "clarification"
        assert result.sql_executed is None


class TestNamedValueFiltering:
    """Regression tests for a third round of gaps found via a review of 9 harder
    sample questions (see the project plan): the mock had no concept of filtering to a
    *named* entity (a division, country, quarter, or item) mentioned in the question,
    and its metric-priority order picked the wrong metric whenever multiple
    metric-sounding words appeared in one sentence."""

    async def test_named_division_becomes_a_filter_not_a_groupby(self, orchestrator, catalog):
        """"for the 'Dhaka' division" must filter to Dhaka and group by the explicitly
        requested "by month" - not group by division (division is consumed as a filter
        value, not a group-by request)."""
        result = await orchestrator.answer(
            "Show me total sales revenue by month for the 'Dhaka' division during 2019.",
            catalog, "s",
        )
        assert result.response_type == "query"
        sql = result.sql_executed
        assert "store_division = 'DHAKA'" in sql
        assert "sale_year = 2019" in sql
        assert "GROUP BY sale_month" in sql
        assert "GROUP BY store_division" not in sql

    async def test_compare_two_named_divisions_groups_by_that_dimension(self, orchestrator, catalog):
        """2 named values on the same dimension = compare mode: IN filter, and that
        dimension IS the group-by target (one row per compared entity)."""
        result = await orchestrator.answer(
            "Compare total sales revenue between 'Dhaka' and 'Chittagong' for Q3 2020.",
            catalog, "s",
        )
        assert result.response_type == "query"
        sql = result.sql_executed
        assert "store_division IN (" in sql
        assert "'DHAKA'" in sql and "'CHITTAGONG'" in sql
        assert "sale_quarter = 'Q3'" in sql
        assert "GROUP BY store_division" in sql
        assert result.row_count == 2

    async def test_named_country_filter_with_explicit_groupby_dimension(self, orchestrator, catalog):
        """Regression: this exact question previously got misrouted into the listing
        feature (starts with "List", contains the dimension keyword "district") and
        returned a bare district list with no revenue and no country filter at all."""
        result = await orchestrator.answer(
            "List total revenue generated by items manufactured in 'China', "
            "broken down by store district.",
            catalog, "s",
        )
        assert result.response_type == "query"
        sql = result.sql_executed
        assert "item_manufacturer_country = 'China'" in sql
        assert "GROUP BY store_district" in sql
        assert "SUM(" in sql
        assert "DISTINCT" not in sql

    async def test_bare_enum_value_becomes_a_filter_without_quotes(self, orchestrator, catalog):
        """"cash" (unquoted) must resolve to a payment_type filter the same way a
        quoted division name does - and "revenue" must win as the metric over the
        incidental "transactions" in "cash transactions" (see test_revenue below)."""
        result = await orchestrator.answer(
            "Show total revenue grouped by payment bank for cash transactions.", catalog, "s"
        )
        assert result.response_type == "query"
        sql = result.sql_executed
        assert "payment_type = 'cash'" in sql
        assert "GROUP BY payment_bank" in sql
        assert "SUM(total_price)" in sql
        assert "COUNT(" not in sql

    async def test_explicit_revenue_wins_over_incidental_transaction_word(self, orchestrator, catalog):
        """Regression: previously answered with a transaction COUNT instead of revenue,
        because "transactions" (part of "cash transactions", describing the filter)
        outranked the explicitly-requested "total revenue" in the old metric priority."""
        result = await orchestrator.answer(
            "Show total revenue grouped by payment bank for cash transactions.", catalog, "s"
        )
        assert "SUM(total_price)" in result.sql_executed or "SUM(total_revenue)" in result.sql_executed
        assert "COUNT(" not in result.sql_executed

    async def test_compound_question_answers_with_the_correct_primary_metric(self, orchestrator, catalog):
        """Regression: "highest total revenue... total sales volume" previously
        answered with units, dropping revenue - the actual primary ask. Full
        multi-metric support (returning both) is out of scope; getting the primary
        metric right is not."""
        result = await orchestrator.answer(
            "Which store division generated the highest total revenue in 2020, "
            "and what was its total sales volume?",
            catalog, "s",
        )
        assert result.response_type == "query"
        assert "SUM(total_revenue)" in result.sql_executed
        assert "sale_year = 2020" in result.sql_executed

    async def test_unusual_but_realistic_question_does_not_crash(self, orchestrator, catalog):
        """A named-value question with no matching data (item name not in the
        database) must still resolve to a valid, guardrailed query with zero rows -
        not raise, and not silently fall back to an unrelated default."""
        result = await orchestrator.answer(
            "Revenue for items called 'Totally Fictional Product'", catalog, "s"
        )
        assert result.response_type == "query"
        assert "item_name ILIKE" in result.sql_executed
        assert result.row_count == 0


class TestSessionStateResolution:
    """New in this round: session state (not just raw turn history) is what lets
    "same as before but by district" and "those" resolve - see
    app.agent.llm_provider.resolve_with_session and app.session.store.SessionStore."""

    async def test_same_as_before_but_different_dimension_inherits_metric_only(
        self, orchestrator, catalog
    ):
        first = await orchestrator.answer("Total revenue by division", catalog, "s1")
        assert first.response_type == "query"
        assert "GROUP BY store_division" in first.sql_executed

        second = await orchestrator.answer("Same as before but by district", catalog, "s1")
        assert second.response_type == "query"
        # The metric is inherited (revenue), the dimension comes from THIS message
        # (district), not the one from turn 1 (division).
        assert "SUM(total_revenue)" in second.sql_executed or "SUM(total_price)" in second.sql_executed
        assert "GROUP BY store_district" in second.sql_executed
        assert "GROUP BY store_division" not in second.sql_executed

    async def test_bare_reference_inherits_everything(self, orchestrator, catalog):
        first = await orchestrator.answer("Total revenue by division in 2020", catalog, "s2")
        assert first.response_type == "query"

        second = await orchestrator.answer("What about those?", catalog, "s2")
        assert second.response_type == "query"
        assert "GROUP BY store_division" in second.sql_executed
        assert "sale_year = 2020" in second.sql_executed

    async def test_fresh_standalone_query_does_not_inherit_stale_state(self, orchestrator, catalog):
        """A message with its own explicit metric AND dimension is a fresh query -
        must not be polluted by a completely unrelated prior turn's filters."""
        first = await orchestrator.answer(
            "Revenue by division for the 'Dhaka' division in 2019", catalog, "s3"
        )
        assert first.response_type == "query"
        assert "store_division = 'DHAKA'" in first.sql_executed

        second = await orchestrator.answer("Units sold by district", catalog, "s3")
        assert second.response_type == "query"
        assert "store_district" in second.sql_executed
        assert "DHAKA" not in second.sql_executed
        assert "sale_year = 2019" not in second.sql_executed

    async def test_bare_metric_fragment_never_inherits_even_with_prior_state(
        self, orchestrator, catalog
    ):
        """The single most important gate in this design: a bare "revenue" must always
        ask for clarification, never silently resolve using a leftover dimension/filter
        from a completely unrelated earlier turn just because session state happens to
        have one lying around."""
        first = await orchestrator.answer("Total revenue by division for 'Dhaka'", catalog, "s4")
        assert first.response_type == "query"

        second = await orchestrator.answer("revenue", catalog, "s4")
        assert second.response_type == "clarification"
        assert second.sql_executed is None

    async def test_narrative_is_prose_not_bullets(self, orchestrator, catalog):
        """Regression guard: the old "- Metric: ... / - Breakdown: ... / - Filters:
        ..." bulleted preamble read as a raw state dump, not an answer - replaced with
        prose that weaves the same context into natural sentences."""
        result = await orchestrator.answer("Total revenue by division in 2020", catalog, "s5")
        assert result.response_type == "query"
        assert "- Metric:" not in result.narrative_text
        assert "- Breakdown:" not in result.narrative_text
        assert "- Filters:" not in result.narrative_text
        assert "2020" in result.narrative_text
        assert "revenue" in result.narrative_text.lower()


class TestMultiYearComparison:
    """Regression tests for a real bug found via live Gemini testing: "compare revenue
    by quarter for 2019 and 2020" used to silently resolve to 2019 only (year
    extraction only ever found the first match), returning a confidently-labeled but
    incomplete answer. See app.agent.llm_provider._extract_years/ResolvedQuery.years.
    """

    async def test_two_years_produces_in_clause_not_first_year_only(self, orchestrator, catalog):
        result = await orchestrator.answer(
            "Compare total revenue by quarter for 2019 and 2020", catalog, "s"
        )
        assert result.response_type == "query"
        sql = result.sql_executed
        assert "sale_year IN (2019, 2020)" in sql
        assert "sale_year = 2019" not in sql
        assert "sale_year = 2020" not in sql

    async def test_two_years_groups_by_year_and_the_explicit_dimension(self, orchestrator, catalog):
        """The explicit "by quarter" breakdown must survive alongside the year
        comparison - not be replaced by it (both are real, distinct requests)."""
        result = await orchestrator.answer(
            "Compare total revenue by quarter for 2019 and 2020", catalog, "s"
        )
        sql = result.sql_executed
        assert "GROUP BY sale_year, sale_quarter" in sql
        assert result.row_count == 8  # 2 years x 4 quarters

    async def test_two_years_without_other_dimension_groups_by_year_alone(self, orchestrator, catalog):
        result = await orchestrator.answer("Compare total revenue for 2019 and 2020", catalog, "s")
        assert result.response_type == "query"
        sql = result.sql_executed
        assert "sale_year IN (2019, 2020)" in sql
        assert "GROUP BY sale_year" in sql
        assert result.row_count == 2

    async def test_single_year_still_uses_equality_not_in_clause(self, orchestrator, catalog):
        """Regression guard: the single-year path (the overwhelmingly common case)
        must be completely unaffected by the multi-year addition."""
        result = await orchestrator.answer("Total revenue by quarter in 2019", catalog, "s")
        sql = result.sql_executed
        assert "sale_year = 2019" in sql
        assert "IN (" not in sql

    async def test_narrative_shows_both_years_not_just_the_first(self, orchestrator, catalog):
        result = await orchestrator.answer(
            "Compare total revenue by quarter for 2019 and 2020", catalog, "s"
        )
        assert "for 2019 and 2020" in result.narrative_text
        assert "2019 in 2019" not in result.narrative_text  # sanity: no mangled duplication

    async def test_narrative_reports_year_over_year_change(self, orchestrator, catalog):
        result = await orchestrator.answer(
            "Compare total revenue by quarter for 2019 and 2020", catalog, "s"
        )
        assert "2019" in result.narrative_text and "2020" in result.narrative_text
        assert "%" in result.narrative_text  # the computed year-over-year delta

    async def test_narrative_describes_both_dimensions_not_just_year(self, orchestrator, catalog):
        """Regression guard for the narrative/chart bug this surfaced: previously only
        the first categorical column (sale_year) was ever consulted, silently dropping
        sale_quarter from both the text summary and the chart."""
        result = await orchestrator.answer(
            "Compare total revenue by quarter for 2019 and 2020", catalog, "s"
        )
        assert "sale year and sale quarter" in result.narrative_text.lower()

    async def test_two_dimension_result_renders_as_a_table_not_a_combined_label_chart(
        self, orchestrator, catalog
    ):
        """A single-series line/bar chart can't represent two independent dimensions
        without collapsing them into one combined label ("2020 Q4"), which reads as a
        flat trend line rather than a real comparison - this should be a table."""
        result = await orchestrator.answer(
            "Compare total revenue by quarter for 2019 and 2020", catalog, "s"
        )
        assert result.visualization.render_chart is True
        assert result.visualization.chart_type == "table"
        assert result.visualization.columns == ["sale_year", "sale_quarter", "total_revenue"]
        assert len(result.visualization.data) == 8


async def test_orchestrator_error_on_repeated_guardrail_failure(orchestrator, catalog, monkeypatch):
    """Sanity check that exhausting retries still raises OrchestratorError rather than
    hanging or returning a malformed result - unrelated to the taxonomy rewrite, kept
    as a smoke test on the retry loop itself."""

    async def _always_bad_sql(*args, **kwargs):
        return "DROP TABLE mv_sales_analysis"

    monkeypatch.setattr(MockLLMProvider, "generate_sql", _always_bad_sql)
    with pytest.raises(OrchestratorError):
        await orchestrator.answer("Total revenue by division", catalog, "s", max_retries=1)
