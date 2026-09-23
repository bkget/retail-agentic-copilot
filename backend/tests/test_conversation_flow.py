"""Multi-turn conversation regressions, replayed against the real semantic layer.

The headline bug: the assistant asked "which time period?", the user answered "2020",
and the assistant asked the exact same question again - forever - because each reply
was classified in isolation with no memory that a clarification was pending. These
tests pin down slot-filling (pending clarification), out-of-scope handling, and the
live reasoning-step stream.
"""

from __future__ import annotations

import pytest

from app.agent.llm_provider import MockLLMProvider
from app.agent.nlu_fallback import QuestionRewriter, RewriteResult
from app.agent.orchestrator import Orchestrator
from app.schema.catalog import load_catalog
from app.session.store import SessionStore


@pytest.fixture
async def catalog(agent_pool):
    return await load_catalog(agent_pool)


@pytest.fixture
def orchestrator(agent_pool):
    return Orchestrator(MockLLMProvider(), agent_pool, SessionStore(ttl_seconds=1800, max_turns=10))


class TestScreenshotConversations:
    async def test_store_by_month_then_year_then_all_time_then_capabilities(self, orchestrator, catalog):
        sid = "shot-1"
        first = await orchestrator.answer(
            "Compare the sales revenue per store and monthly distribution", catalog, sid
        )
        assert first.response_type == "query"
        assert "GROUP BY store_district, sale_month" in first.sql_executed
        assert "sale_year" not in first.sql_executed.split("GROUP BY")[0].split("FROM")[0]
        assert first.row_count <= 8 * 12  # top-8 districts x 12 months, never truncated
        assert first.visualization.chart_type == "multi_line"
        assert "grouped by district" in first.narrative_text  # the store approximation is disclosed

        second = await orchestrator.answer("2020", catalog, sid)
        assert second.response_type == "query"
        assert "sale_year = 2020" in second.sql_executed
        assert "GROUP BY store_district, sale_month" in second.sql_executed

        third = await orchestrator.answer("GO WITH ALL TIME", catalog, sid)
        assert third.response_type == "query"
        assert "sale_year = 2020" not in third.sql_executed
        assert "GROUP BY store_district, sale_month" in third.sql_executed

        fourth = await orchestrator.answer("what question kind you can answer?", catalog, sid)
        assert fourth.response_type == "conversational"
        assert fourth.sql_executed is None
        assert "revenue" in fourth.narrative_text.lower()

    async def test_stores_per_division_and_district_asks_measure_once_then_answers(
        self, orchestrator, catalog
    ):
        sid = "shot-2"
        first = await orchestrator.answer(
            "how to compare Stores per divisions, and districts on 2020", catalog, sid
        )
        assert first.response_type == "clarification"
        assert "measure" in first.narrative_text.lower()
        assert first.suggestions  # quick replies offered

        second = await orchestrator.answer("Revenue", catalog, sid)
        assert second.response_type == "query"
        assert "GROUP BY store_division, store_district" in second.sql_executed
        assert "sale_year = 2020" in second.sql_executed


class TestClarificationSlotFilling:
    @pytest.mark.parametrize(
        "reply, expect_in_sql, expect_not_in_sql",
        [
            ("2020", "sale_year = 2020", "GROUP BY"),
            ("all time", "SUM(", "sale_year"),
            ("All-time total", "SUM(", "sale_year"),
            ("By division", "GROUP BY store_division", "sale_year"),
            ("By month in 2021", "GROUP BY sale_month", "sale_year = 2020"),
            ("Top 10 items", "GROUP BY item_name", "sale_year"),
        ],
    )
    async def test_answer_to_breakdown_question_is_merged_not_re_asked(
        self, orchestrator, catalog, reply, expect_in_sql, expect_not_in_sql
    ):
        sid = f"fill-{reply}"
        first = await orchestrator.answer("revenue", catalog, sid)
        assert first.response_type == "clarification"

        second = await orchestrator.answer(reply, catalog, sid)
        assert second.response_type == "query", second.narrative_text
        assert expect_in_sql in second.sql_executed
        assert expect_not_in_sql not in second.sql_executed

    async def test_never_asks_for_the_measure_twice(self, orchestrator, catalog):
        sid = "no-loop"
        first = await orchestrator.answer("Only for 2020", catalog, sid)
        assert first.response_type == "clarification"
        second = await orchestrator.answer("by division", catalog, sid)
        assert second.response_type == "query"
        assert "GROUP BY store_division" in second.sql_executed
        assert "didn't name a measure" in second.narrative_text

    async def test_new_question_while_clarification_pending_is_answered_on_its_own(
        self, orchestrator, catalog
    ):
        sid = "switch"
        await orchestrator.answer("revenue", catalog, sid)
        result = await orchestrator.answer("Units sold by district in 2019", catalog, sid)
        assert result.response_type == "query"
        assert "store_district" in result.sql_executed
        assert "sale_year = 2019" in result.sql_executed

    async def test_greeting_while_pending_does_not_break_the_flow(self, orchestrator, catalog):
        sid = "greet-mid"
        await orchestrator.answer("revenue", catalog, sid)
        result = await orchestrator.answer("thanks", catalog, sid)
        assert result.response_type == "conversational"


class TestOutOfScope:
    @pytest.mark.parametrize(
        "message", ["who won the world cup?", "write me a poem about the sea", "what's the weather"]
    )
    async def test_unrelated_question_says_it_cannot_answer_and_guides(self, orchestrator, catalog, message):
        result = await orchestrator.answer(message, catalog, f"oos-{message}")
        assert result.response_type == "conversational"
        assert result.sql_executed is None
        assert "can't answer" in result.narrative_text
        assert "revenue" in result.narrative_text.lower()  # tells the user what to ask instead
        assert len(result.suggestions) >= 3

    async def test_unsupported_concept_is_reshaped_and_confirmed(self, orchestrator, catalog):
        sid = "profit"
        first = await orchestrator.answer("What is the profit by division in 2020?", catalog, sid)
        assert first.response_type == "clarification"
        assert "cost" in first.narrative_text.lower()
        assert "Total revenue by division in 2020" in first.narrative_text

        second = await orchestrator.answer("Yes, show that", catalog, sid)
        assert second.response_type == "query"
        assert "GROUP BY store_division" in second.sql_executed
        assert "sale_year = 2020" in second.sql_executed

    async def test_unsupported_concept_declined(self, orchestrator, catalog):
        sid = "cust"
        first = await orchestrator.answer("Who are our most loyal customers?", catalog, sid)
        assert first.response_type == "clarification"
        assert "privacy" in first.narrative_text.lower()
        second = await orchestrator.answer("no thanks", catalog, sid)
        assert second.response_type == "conversational"
        assert second.sql_executed is None

    async def test_greeting_word_inside_a_real_question_is_not_a_greeting(self, orchestrator, catalog):
        result = await orchestrator.answer("hi, what is total revenue by division?", catalog, "hi-q")
        assert result.response_type == "query"


class TestReasoningSteps:
    async def test_query_emits_the_full_step_trace(self, orchestrator, catalog):
        steps: list[dict] = []

        async def on_step(step: dict) -> None:
            steps.append(step)

        await orchestrator.answer("Total revenue by division in 2020", catalog, "steps", on_step=on_step)
        done_ids = [s["id"] for s in steps if s["status"] == "done"]
        for expected in ("understand", "plan", "sql", "guardrail", "execute", "answer"):
            assert expected in done_ids
        assert any(s["status"] == "running" for s in steps)  # live, not only after the fact

    async def test_follow_up_reports_the_context_it_used(self, orchestrator, catalog):
        steps: list[dict] = []

        async def on_step(step: dict) -> None:
            steps.append(step)

        await orchestrator.answer("Total revenue by division", catalog, "ctx")
        await orchestrator.answer("Only for 2021", catalog, "ctx", on_step=on_step)
        context = [s for s in steps if s["id"] == "context"]
        assert context and "total revenue" in context[0]["detail"]


class _FakeRewriter(QuestionRewriter):
    def __init__(self, result: RewriteResult | None):
        self.result = result
        self.calls = 0

    async def rewrite(self, question, history, year_range):
        self.calls += 1
        return self.result


class TestLlmFallback:
    async def test_rewrite_is_fed_back_through_the_deterministic_pipeline(self, agent_pool, catalog):
        rewriter = _FakeRewriter(RewriteResult(True, "Total revenue by division in 2021"))
        orch = Orchestrator(MockLLMProvider(), agent_pool, SessionStore(1800, 10), rewriter)
        result = await orch.answer("how much dough did each region pull in last yr", catalog, "llm-1")
        assert rewriter.calls == 1
        assert result.response_type == "query"
        assert "GROUP BY store_division" in result.sql_executed
        assert "sale_year = 2021" in result.sql_executed

    async def test_llm_saying_out_of_scope_gives_the_guidance_reply(self, agent_pool, catalog):
        orch = Orchestrator(
            MockLLMProvider(), agent_pool, SessionStore(1800, 10), _FakeRewriter(RewriteResult(False, None))
        )
        result = await orch.answer("tell me a joke", catalog, "llm-2")
        assert result.response_type == "conversational"
        assert "can't answer" in result.narrative_text

    async def test_llm_unavailable_degrades_to_deterministic_reply(self, agent_pool, catalog):
        orch = Orchestrator(MockLLMProvider(), agent_pool, SessionStore(1800, 10), _FakeRewriter(None))
        result = await orch.answer("tell me a joke", catalog, "llm-3")
        assert result.response_type == "conversational"
        assert result.suggestions

    async def test_rule_based_questions_never_call_the_llm(self, agent_pool, catalog):
        rewriter = _FakeRewriter(RewriteResult(True, "irrelevant"))
        orch = Orchestrator(MockLLMProvider(), agent_pool, SessionStore(1800, 10), rewriter)
        await orch.answer("Total revenue by division", catalog, "llm-4")
        assert rewriter.calls == 0


class TestDataCoverage:
    async def test_year_outside_the_data_offers_the_nearest_valid_year(self, orchestrator, catalog):
        sid = "range"
        first = await orchestrator.answer("Total revenue by division in 2024", catalog, sid)
        assert first.response_type == "clarification"
        assert first.sql_executed is None
        assert f"{catalog.year_min} to {catalog.year_max}" in first.narrative_text
        assert f"in {catalog.year_max}" in first.narrative_text

        second = await orchestrator.answer(first.suggestions[0], catalog, sid)
        assert second.response_type == "query"
        assert f"sale_year = {catalog.year_max}" in second.sql_executed


class TestContinuousSession:
    """Found by replaying the screenshot turns in ONE session against the live API."""

    async def test_unsupported_question_while_pending_is_not_merged_as_an_answer(
        self, orchestrator, catalog
    ):
        sid = "cont-1"
        assert (await orchestrator.answer("revenue", catalog, sid)).response_type == "clarification"
        profit = await orchestrator.answer("What is the profit by division?", catalog, sid)
        assert profit.response_type == "clarification"
        assert profit.sql_executed is None
        assert "cost" in profit.narrative_text.lower()
        yes = await orchestrator.answer("yes", catalog, sid)
        assert yes.response_type == "query"
        assert "GROUP BY store_division" in yes.sql_executed

    async def test_bare_yes_with_nothing_pending_is_friendly_not_out_of_scope(self, orchestrator, catalog):
        result = await orchestrator.answer("yes", catalog, "cont-2")
        assert result.response_type == "conversational"
        assert "can't answer" not in result.narrative_text
        assert result.suggestions

    async def test_division_district_narrative_reads_naturally(self, orchestrator, catalog):
        result = await orchestrator.answer("Total revenue per division and district in 2020", catalog, "cont-3")
        assert "GROUP BY store_division, store_district" in result.sql_executed
        assert "DHAKA DHAKA" not in result.narrative_text
        assert "(DHAKA)" in result.narrative_text
