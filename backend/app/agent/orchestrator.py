"""Ties the pipeline together: intent classification -> (conversational reply |
clarifying question | NL question -> LLM-generated SQL -> AST guardrail -> execution ->
deterministic narrative/visualization). Every question is classified before any SQL is
attempted - greetings and "what can you do" questions never reach the database, and a
genuinely vague question gets a clarifying question back instead of a guessed default.
Only genuine analytical questions retry generation up to `max_retries` times, feeding the
guardrail/DB error back to the LLM as context - the self-correction loop the original
spec was missing (one-shot generation with no retry path measurably lowers NL2SQL
accuracy).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import asyncpg
from opentelemetry.trace import Span, Status, StatusCode

from app.agent.llm_provider import ConversationTurn, Intent, LLMProvider
from app.agent.narrative import build_narrative
from app.agent.visualization import VisualizationConfig, build_visualization
from app.observability.tracing import get_tracer
from app.schema.catalog import SchemaCatalog
from app.security.ast_guardrail import MAX_ROW_LIMIT, GuardrailViolation, validate_and_reserialize_sql

_tracer = get_tracer()

DEFAULT_FORMATTING = {"currency": "$", "unit": "units", "decimals": 0}


class OrchestratorError(RuntimeError):
    def __init__(self, message: str, attempts: list[str]):
        super().__init__(message)
        self.attempts = attempts


@dataclass(frozen=True)
class AnswerResult:
    response_type: str  # "conversational" | "clarification" | "query"
    narrative_text: str
    formatting: dict
    sql_executed: str | None = None
    row_count: int | None = None
    is_truncated: bool | None = None
    visualization: VisualizationConfig | None = None
    metrics: dict[str, int] = field(default_factory=dict)


class Orchestrator:
    def __init__(self, llm: LLMProvider, pool: asyncpg.Pool):
        self._llm = llm
        self._pool = pool

    async def answer(
        self,
        question: str,
        catalog: SchemaCatalog,
        history: list[ConversationTurn],
        max_retries: int = 2,
    ) -> AnswerResult:
        with _tracer.start_as_current_span("orchestrator.answer") as root_span:
            root_span.set_attribute("copilot.question", question)
            root_span.set_attribute("copilot.max_retries", max_retries)

            t0 = time.monotonic()
            with _tracer.start_as_current_span("llm.classify") as span:
                intent_result = await self._llm.classify(question, history)
                span.set_attribute("copilot.intent", intent_result.intent.value)
            classify_ms = int((time.monotonic() - t0) * 1000)
            root_span.set_attribute("copilot.intent", intent_result.intent.value)

            if intent_result.intent in (Intent.GREETING, Intent.HELP):
                return AnswerResult(
                    response_type="conversational",
                    narrative_text=intent_result.reply or "How can I help you today?",
                    formatting=DEFAULT_FORMATTING,
                    metrics=_zeroed_metrics(classify_ms),
                )

            if intent_result.intent == Intent.CLARIFICATION_NEEDED:
                return AnswerResult(
                    response_type="clarification",
                    narrative_text=intent_result.clarifying_question or "Could you clarify what you'd like to know?",
                    formatting=DEFAULT_FORMATTING,
                    metrics=_zeroed_metrics(classify_ms),
                )

            if intent_result.intent == Intent.EXPLAIN_PREVIOUS:
                # classify() only returns this intent when it already found a cached
                # narrative in history (see MockLLMProvider.classify), so this should
                # always find one - the "no result yet" fallback text only matters if a
                # provider implementation doesn't enforce that precondition.
                previous_narrative = next(
                    (t.narrative_text for t in reversed(history) if t.narrative_text), None
                )
                text = (
                    f"Sure - here's that again: {previous_narrative}"
                    if previous_narrative
                    else "I don't have a previous result to explain yet - ask me something first."
                )
                return AnswerResult(
                    response_type="conversational",
                    narrative_text=text,
                    formatting=DEFAULT_FORMATTING,
                    metrics=_zeroed_metrics(classify_ms),
                )

            if intent_result.intent == Intent.DATA_COVERAGE:
                # Built here, not by the provider: classify() doesn't receive `catalog`
                # (only generate_sql does), and Orchestrator already owns it.
                if catalog.year_min is not None:
                    text = f"I have sales data from {catalog.year_min} to {catalog.year_max}."
                else:
                    text = "I don't currently know the exact date range of the data I have."
                return AnswerResult(
                    response_type="conversational",
                    narrative_text=text,
                    formatting=DEFAULT_FORMATTING,
                    metrics=_zeroed_metrics(classify_ms),
                )

            return await self._answer_query(
                question, catalog, history, max_retries, root_span, classify_ms
            )

    async def _answer_query(
        self,
        question: str,
        catalog: SchemaCatalog,
        history: list[ConversationTurn],
        max_retries: int,
        root_span: Span,
        classify_ms: int,
    ) -> AnswerResult:
        t_total_start = time.monotonic()

        error_feedback: str | None = None
        attempts: list[str] = []
        llm_ms = classify_ms
        guardrail_ms = sql_ms = 0

        for attempt in range(max_retries + 1):
            with _tracer.start_as_current_span("llm.generate_sql") as span:
                span.set_attribute("copilot.attempt", attempt + 1)
                t0 = time.monotonic()
                raw_sql = await self._llm.generate_sql(
                    question, catalog, history, error_feedback
                )
                llm_ms += int((time.monotonic() - t0) * 1000)

            with _tracer.start_as_current_span("guardrail.validate") as span:
                t0 = time.monotonic()
                try:
                    safe_sql = validate_and_reserialize_sql(raw_sql)
                except GuardrailViolation as exc:
                    guardrail_ms += int((time.monotonic() - t0) * 1000)
                    span.set_status(Status(StatusCode.ERROR, str(exc)))
                    attempts.append(f"attempt {attempt + 1}: guardrail rejected - {exc}")
                    error_feedback = str(exc)
                    continue
                guardrail_ms += int((time.monotonic() - t0) * 1000)
                span.set_attribute("copilot.sql", safe_sql)

            with _tracer.start_as_current_span("sql.execute") as span:
                t0 = time.monotonic()
                try:
                    async with self._pool.acquire() as conn:
                        records = await conn.fetch(safe_sql)
                except asyncpg.PostgresError as exc:
                    sql_ms += int((time.monotonic() - t0) * 1000)
                    span.set_status(Status(StatusCode.ERROR, str(exc)))
                    attempts.append(f"attempt {attempt + 1}: SQL execution failed - {exc}")
                    error_feedback = str(exc)
                    continue
                sql_ms += int((time.monotonic() - t0) * 1000)
                span.set_attribute("copilot.row_count", len(records))

            rows = [dict(r) for r in records]
            columns = list(records[0].keys()) if records else []
            total_ms = int((time.monotonic() - t_total_start) * 1000) + classify_ms
            root_span.set_attribute("copilot.row_count", len(rows))
            root_span.set_attribute("copilot.attempts_used", attempt + 1)

            narrative = build_narrative(question, columns, rows)
            return AnswerResult(
                response_type="query",
                narrative_text=narrative.text,
                formatting=narrative.formatting,
                sql_executed=safe_sql,
                row_count=len(rows),
                is_truncated=len(rows) >= MAX_ROW_LIMIT,
                visualization=build_visualization(question, columns, rows),
                metrics={
                    "llm_ms": llm_ms,
                    "guardrail_ms": guardrail_ms,
                    "sql_ms": sql_ms,
                    "total_ms": total_ms,
                },
            )

        root_span.set_status(Status(StatusCode.ERROR, "exhausted retries"))
        raise OrchestratorError(
            f"Failed to produce a valid, executable query after {max_retries + 1} attempts.",
            attempts=attempts,
        )


def _zeroed_metrics(classify_ms: int) -> dict[str, int]:
    return {"llm_ms": classify_ms, "guardrail_ms": 0, "sql_ms": 0, "total_ms": classify_ms}
