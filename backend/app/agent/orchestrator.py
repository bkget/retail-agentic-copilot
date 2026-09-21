"""Ties the pipeline together, every turn, in this order: classify_input -> route on
intent -> (for DATABASE_QUERY, or a CLARIFICATION/UNKNOWN that references prior
context) get_session -> map_terms_to_columns -> resolve_with_session ->
generate_sql -> AST guardrail -> run_sql -> deterministic narrative/visualization ->
update_session. Greetings and "what can you do" questions never reach the database,
and a genuinely vague or unresolvable question gets a clarifying question back instead
of a guessed default - that gate is the single most important property of this class,
see `_should_attempt_resolution` and the "no metric resolved" check in `answer()`.

Only genuine analytical questions retry generation, up to `max_retries` times, feeding
the guardrail/DB error back to the LLM as context - the self-correction loop the
original spec was missing (one-shot generation with no retry path measurably lowers
NL2SQL accuracy).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace

import asyncpg
from opentelemetry.trace import Span, Status, StatusCode

from app.agent.llm_provider import (
    CLARIFYING_REPLY_TIME_PERIOD,
    GREETING_REPLY,
    SCHEMA_INFO_REPLY,
    UNKNOWN_REPLY,
    ClassificationResult,
    ConversationTurn,
    Intent,
    LLMProvider,
    ResolvedQuery,
    SessionState,
    is_data_coverage_question,
    resolve_with_session,
)
from app.agent.narrative import build_narrative
from app.agent.visualization import VisualizationConfig, build_visualization
from app.observability.tracing import get_tracer
from app.schema.catalog import SchemaCatalog
from app.security.ast_guardrail import MAX_ROW_LIMIT, GuardrailViolation, validate_and_reserialize_sql
from app.session.store import SessionContext, SessionStore

_tracer = get_tracer()

DEFAULT_FORMATTING = {"currency": "$", "unit": "units", "decimals": 0}

# Shown when a question routes to DATABASE_QUERY purely on the strength of
# references_previous (e.g. "those", "same as before") but session state turns out to
# have nothing to offer - a fresh session's first message can't be a follow-up.
NOTHING_TO_REFER_TO_REPLY = (
    "This looks like a follow-up, but I don't have a previous question in this "
    "session to build on yet. What would you like to know?"
)


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
    def __init__(self, llm: LLMProvider, pool: asyncpg.Pool, session_store: SessionStore):
        self._llm = llm
        self._pool = pool
        self._session_store = session_store

    async def answer(
        self,
        question: str,
        catalog: SchemaCatalog,
        session_id: str,
        max_retries: int = 2,
    ) -> AnswerResult:
        with _tracer.start_as_current_span("orchestrator.answer") as root_span:
            root_span.set_attribute("copilot.question", question)
            root_span.set_attribute("copilot.session_id", session_id)
            root_span.set_attribute("copilot.max_retries", max_retries)

            session = self._session_store.get_session(session_id)

            t0 = time.monotonic()
            with _tracer.start_as_current_span("llm.classify_input") as span:
                classification = await self._llm.classify_input(question)
                span.set_attribute("copilot.intent", classification.intent.value)
                span.set_attribute("copilot.confidence", classification.confidence)
            classify_ms = int((time.monotonic() - t0) * 1000)
            root_span.set_attribute("copilot.intent", classification.intent.value)

            if classification.intent == Intent.GREETING:
                return self._respond_conversational(
                    session_id, session, question, GREETING_REPLY, classify_ms
                )

            if classification.intent == Intent.SCHEMA_INFO:
                text = SCHEMA_INFO_REPLY
                if is_data_coverage_question(question):
                    text = (
                        f"I have sales data from {catalog.year_min} to {catalog.year_max}."
                        if catalog.year_min is not None
                        else "I don't currently know the exact date range of the data I have."
                    )
                return self._respond_conversational(session_id, session, question, text, classify_ms)

            if not _should_attempt_resolution(classification):
                is_clarification = classification.intent == Intent.CLARIFICATION
                text = CLARIFYING_REPLY_TIME_PERIOD if is_clarification else UNKNOWN_REPLY
                return self._respond(
                    session_id,
                    session,
                    question,
                    response_type="clarification" if is_clarification else "conversational",
                    text=text,
                    classify_ms=classify_ms,
                )

            with _tracer.start_as_current_span("llm.map_terms_to_columns") as span:
                mapping = await self._llm.map_terms_to_columns(question, catalog)
                span.set_attribute("copilot.mapped_columns", len(mapping.mappings))

            resolved = resolve_with_session(mapping, classification.extracted_signals, session.state)

            if not resolved.is_listing and resolved.metric_alias is None:
                # Either a bare reference ("those") with nothing in session state to
                # build on, or a mapping that genuinely found no usable metric -
                # ask instead of defaulting to an arbitrary total.
                text = (
                    NOTHING_TO_REFER_TO_REPLY
                    if classification.extracted_signals.references_previous and not session.history
                    else CLARIFYING_REPLY_TIME_PERIOD
                )
                return self._respond(
                    session_id, session, question, "clarification", text, classify_ms
                )

            return await self._answer_query(
                question, catalog, resolved, session_id, session, max_retries, root_span, classify_ms
            )

    def _respond_conversational(
        self, session_id: str, session: SessionContext, question: str, text: str, classify_ms: int
    ) -> AnswerResult:
        return self._respond(session_id, session, question, "conversational", text, classify_ms)

    def _respond(
        self,
        session_id: str,
        session: SessionContext,
        question: str,
        response_type: str,
        text: str,
        classify_ms: int,
    ) -> AnswerResult:
        # Greetings, schema-info, and clarifying questions don't change what the agent
        # believes the conversation is "about" - state is carried over untouched, only
        # history gains a turn (with no cached narrative to replay, since nothing was
        # actually answered).
        self._session_store.update_session(
            session_id,
            session.state,
            ConversationTurn(question=question, sql=None, row_count=None, narrative_text=None),
        )
        return AnswerResult(
            response_type=response_type,
            narrative_text=text,
            formatting=DEFAULT_FORMATTING,
            metrics=_zeroed_metrics(classify_ms),
        )

    async def _answer_query(
        self,
        question: str,
        catalog: SchemaCatalog,
        resolved: ResolvedQuery,
        session_id: str,
        session: SessionContext,
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
                raw_sql = await self._llm.generate_sql(question, catalog, resolved, error_feedback)
                llm_ms += int((time.monotonic() - t0) * 1000)
                # Logged unconditionally, not just on a guardrail pass - otherwise a
                # rejected attempt's actual generated SQL is never visible anywhere,
                # making "why did this fail" undiagnosable from the trace log alone.
                span.set_attribute("copilot.raw_sql", raw_sql)

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
                span.set_attribute("copilot.sql", safe_sql)
                t0 = time.monotonic()
                try:
                    rows, columns = await self._run_sql(safe_sql)
                except asyncpg.PostgresError as exc:
                    sql_ms += int((time.monotonic() - t0) * 1000)
                    span.set_status(Status(StatusCode.ERROR, str(exc)))
                    attempts.append(f"attempt {attempt + 1}: SQL execution failed - {exc}")
                    error_feedback = str(exc)
                    continue
                sql_ms += int((time.monotonic() - t0) * 1000)
                span.set_attribute("copilot.row_count", len(rows))

            total_ms = int((time.monotonic() - t_total_start) * 1000) + classify_ms
            root_span.set_attribute("copilot.row_count", len(rows))
            root_span.set_attribute("copilot.attempts_used", attempt + 1)

            narrative = build_narrative(resolved, columns, rows)

            self._update_state_after_query(session_id, session.state, resolved, question, safe_sql, len(rows), narrative.text)

            return AnswerResult(
                response_type="query",
                narrative_text=narrative.text,
                formatting=narrative.formatting,
                sql_executed=safe_sql,
                row_count=len(rows),
                is_truncated=len(rows) >= MAX_ROW_LIMIT,
                visualization=build_visualization(columns, rows),
                metrics={
                    "llm_ms": llm_ms,
                    "guardrail_ms": guardrail_ms,
                    "sql_ms": sql_ms,
                    "total_ms": total_ms,
                },
            )

        # `attempts` (the actual guardrail-rejection/SQL-execution reason for each try)
        # was previously only ever attached to the raised exception's .attempts
        # attribute - str(exc) (what both the client error event and OTel's default
        # exception logging use) never included it, making every "failed after N
        # attempts" report undiagnosable after the fact. Recorded on the span
        # explicitly so it's actually visible in the trace log.
        root_span.set_attribute("copilot.failed_attempts", attempts)
        root_span.set_status(Status(StatusCode.ERROR, "exhausted retries"))
        raise OrchestratorError(
            f"Failed to produce a valid, executable query after {max_retries + 1} attempts.",
            attempts=attempts,
        )

    async def _run_sql(self, sql: str) -> tuple[list[dict], list[str]]:
        """The `run_sql` tool: thin, explicitly-named wrapper around fetching an
        already-guardrail-validated query - never called with anything else."""
        async with self._pool.acquire() as conn:
            records = await conn.fetch(sql)
        rows = [dict(r) for r in records]
        columns = list(records[0].keys()) if records else []
        return rows, columns

    def _update_state_after_query(
        self,
        session_id: str,
        state: SessionState,
        resolved: ResolvedQuery,
        question: str,
        sql: str,
        row_count: int,
        narrative_text: str,
    ) -> None:
        if resolved.is_listing:
            # A listing detour ("what districts are available") doesn't change what
            # the ongoing analytical thread is "about" - leave state as-is so a later
            # "same as before" still refers to the last real query, not the listing.
            new_state = state
        else:
            new_state = SessionState(
                last_metric_alias=resolved.metric_alias,
                last_dimension=resolved.dimension,
                last_year=resolved.year,
                active_filters={f.column: f for f in resolved.filters},
                output_preference=state.output_preference,
            )
        self._session_store.update_session(
            session_id,
            new_state,
            ConversationTurn(question=question, sql=sql, row_count=row_count, narrative_text=narrative_text),
        )


def _should_attempt_resolution(classification: ClassificationResult) -> bool:
    """Gates every call to map_terms_to_columns/resolve_with_session. A bare metric
    fragment ("revenue") must always ask a clarifying question, never silently resolve
    to an all-time total just because session state happens to have a prior dimension
    or filter lying around - that "confidently wrong instead of asking" failure mode is
    exactly what this taxonomy exists to prevent. Resolution is only attempted when the
    message is a genuine analytical question on its own (DATABASE_QUERY), or when it's
    ambiguous/unrecognized but explicitly points at prior context (references_previous)
    - in which case resolve_with_session gets a chance to fill the gap from session
    state, and answer() itself falls back to a clarifying question if that comes up
    empty."""
    if classification.intent == Intent.DATABASE_QUERY:
        return True
    return classification.intent in (Intent.CLARIFICATION, Intent.UNKNOWN) and (
        classification.extracted_signals.references_previous
    )


def _zeroed_metrics(classify_ms: int) -> dict[str, int]:
    return {"llm_ms": classify_ms, "guardrail_ms": 0, "sql_ms": 0, "total_ms": classify_ms}
