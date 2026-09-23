"""Ties the pipeline together, every turn:

    [pending clarification?] -> classify_input -> route on intent
        GREETING / SCHEMA_INFO           -> direct reply, never touches the database
        unsupported concept (profit...)  -> explain + offer the closest answerable question
        fragment ("revenue")             -> ask how to break it down   (sets pending)
        no measure ("by district")       -> ask which measure          (sets pending)
        unrecognized                     -> [optional LLM rewrite] -> out-of-scope reply
        analytical question              -> map terms -> merge with session state ->
                                            generate SQL -> AST guardrail -> execute ->
                                            deterministic narrative/visualization

Conversation handling (the fix for the "which time period?" loop): when the assistant
asks a clarifying question it stores a PendingClarification in session state. The next
message is first interpreted as an *answer* to it - "2020", "all time", "by month",
"revenue", "yes" - and merged with the partially-understood original question. A
clarification is never asked twice in a row: if the answer still leaves a gap, a
sensible default is used and stated in the reply.

Every stage reports progress through `on_step`, which the API streams to the UI as the
live "thinking" trace.
"""

from __future__ import annotations

import asyncio
import time
import weakref
from dataclasses import dataclass, field, replace
from typing import Awaitable, Callable

import asyncpg
from opentelemetry.trace import Span, Status, StatusCode

from app.agent import scope
from app.agent.llm_provider import (
    GREETING_REPLY,
    SCHEMA_INFO_REPLY,
    ClassificationResult,
    ConversationTurn,
    Intent,
    LLMProvider,
    PendingClarification,
    ResolvedQuery,
    SessionState,
    TermMappingResult,
    _word_in,
    is_data_coverage_question,
    resolve_with_session,
    series_limit_for,
)
from app.agent.narrative import build_narrative
from app.agent.nlu_fallback import QuestionRewriter
from app.agent.visualization import VisualizationConfig, build_visualization
from app.observability.tracing import get_tracer
from app.schema.catalog import SchemaCatalog
from app.security.ast_guardrail import MAX_ROW_LIMIT, GuardrailViolation, validate_and_reserialize_sql
from app.session.store import BaseSessionStore, SessionContext

_tracer = get_tracer()

DEFAULT_FORMATTING = {"currency": "$", "unit": "units", "decimals": 0}

NOTHING_TO_REFER_TO_REPLY = (
    "This looks like a follow-up, but I don't have a previous question in this "
    "session to build on yet. What would you like to know?"
)

StepCallback = Callable[[dict], Awaitable[None]]

_INTENT_DETAIL = {
    Intent.GREETING: "Greeting - no data needed",
    Intent.SCHEMA_INFO: "A question about what I can do",
    Intent.DATABASE_QUERY: "An analytical question about the sales data",
    Intent.CLARIFICATION: "A partial question - some details are missing",
    Intent.UNKNOWN: "Doesn't match anything in the sales data vocabulary",
}


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
    suggestions: list[str] = field(default_factory=list)
    interpreted_as: str | None = None  # canonical question actually answered


class _Steps:
    """Emits {id, label, detail, status, elapsed_ms} progress events. A no-op when no
    callback is given (tests, eval harness), and never lets a UI hiccup break a turn."""

    def __init__(self, callback: StepCallback | None):
        self._cb = callback
        self._started: dict[str, float] = {}

    async def _emit(self, payload: dict) -> None:
        if self._cb is None:
            return
        try:
            await self._cb(payload)
        except Exception:  # pragma: no cover - defensive
            pass

    async def start(self, step_id: str, label: str, detail: str | None = None) -> None:
        self._started[step_id] = time.monotonic()
        await self._emit({"id": step_id, "label": label, "detail": detail, "status": "running"})

    async def done(self, step_id: str, label: str, detail: str | None = None, status: str = "done") -> None:
        t0 = self._started.pop(step_id, None)
        elapsed = int((time.monotonic() - t0) * 1000) if t0 is not None else None
        await self._emit(
            {"id": step_id, "label": label, "detail": detail, "status": status, "elapsed_ms": elapsed}
        )


@dataclass
class _Turn:
    """Per-request context threaded through the routing helpers."""

    question: str  # what the user actually typed (stored in history)
    catalog: SchemaCatalog
    session_id: str
    session: SessionContext
    steps: _Steps
    root_span: Span
    max_retries: int
    classify_ms: int = 0

    @property
    def year_range(self) -> str | None:
        c = self.catalog
        return f"{c.year_min}-{c.year_max}" if c.year_min is not None else None


class Orchestrator:
    def __init__(
        self,
        llm: LLMProvider,
        pool: asyncpg.Pool,
        session_store: BaseSessionStore,
        rewriter: QuestionRewriter | None = None,
    ):
        self._llm = llm
        self._pool = pool
        self._session_store = session_store
        self._rewriter = rewriter
        # Serializes turns within one session (two tabs, a double submit) so state
        # updates can't interleave; different sessions never block each other.
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()

    # ------------------------------------------------------------------ entry

    async def answer(
        self,
        question: str,
        catalog: SchemaCatalog,
        session_id: str,
        max_retries: int = 2,
        on_step: StepCallback | None = None,
    ) -> AnswerResult:
        lock = self._locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[session_id] = lock
        async with lock:
            return await self._answer_locked(question, catalog, session_id, max_retries, on_step)

    async def _answer_locked(
        self, question: str, catalog: SchemaCatalog, session_id: str, max_retries: int, on_step
    ) -> AnswerResult:
        with _tracer.start_as_current_span("orchestrator.answer") as root_span:
            root_span.set_attribute("copilot.question", question)
            root_span.set_attribute("copilot.session_id", session_id)
            steps = _Steps(on_step)
            session = await self._session_store.get_session(session_id)
            turn = _Turn(question, catalog, session_id, session, steps, root_span, max_retries)

            classification = await self._classify(turn, question)

            pending = session.state.pending
            # Whatever happens next, the old clarification is consumed by this turn.
            turn.session = SessionContext(
                history=session.history, state=replace(session.state, pending=None)
            )
            if pending is not None:
                result = await self._resolve_pending(turn, classification, pending)
                if result is not None:
                    return result

            return await self._route(turn, question, classification, allow_rewrite=True)

    async def _classify(self, turn: _Turn, text: str) -> ClassificationResult:
        await turn.steps.start("understand", "Understanding your question")
        t0 = time.monotonic()
        with _tracer.start_as_current_span("llm.classify_input") as span:
            classification = await self._llm.classify_input(text)
            span.set_attribute("copilot.intent", classification.intent.value)
        turn.classify_ms += int((time.monotonic() - t0) * 1000)
        turn.root_span.set_attribute("copilot.intent", classification.intent.value)
        await turn.steps.done("understand", "Understanding your question", _INTENT_DETAIL[classification.intent])
        return classification

    async def _map(self, turn: _Turn, text: str) -> TermMappingResult:
        with _tracer.start_as_current_span("llm.map_terms_to_columns") as span:
            mapping = await self._llm.map_terms_to_columns(text, turn.catalog)
            span.set_attribute("copilot.mapped_columns", len(mapping.mappings))
        return mapping

    # ---------------------------------------------------------------- routing

    async def _route(
        self, turn: _Turn, text: str, classification: ClassificationResult, allow_rewrite: bool
    ) -> AnswerResult:
        intent = classification.intent
        signals = classification.extracted_signals
        catalog = turn.catalog

        if intent == Intent.GREETING:
            return await self._respond(
                turn, "conversational", GREETING_REPLY, scope.example_questions(catalog.year_max)
            )

        if intent == Intent.SCHEMA_INFO:
            text_out = SCHEMA_INFO_REPLY
            if is_data_coverage_question(text):
                text_out = (
                    f"I have sales data from {catalog.year_min} to {catalog.year_max}."
                    if catalog.year_min is not None
                    else "I don't currently know the exact date range of the data I have."
                )
            return await self._respond(
                turn, "conversational", text_out, scope.example_questions(catalog.year_max)
            )

        unsupported = scope.detect_unsupported(text, turn.year_range)
        if unsupported is not None:
            return await self._offer_reshape(turn, text, unsupported)

        if not _should_attempt_resolution(classification):
            if intent == Intent.CLARIFICATION:
                # A bare metric fragment ("revenue", "show me sales") - ask how to
                # slice it, and remember what we already know for the answer.
                mapping = await self._map(turn, text)
                metric = mapping.metric_intent or "total_revenue"
                partial = ResolvedQuery(metric_alias=metric, filters=mapping.filters, notes=mapping.notes)
                return await self._respond(
                    turn,
                    "clarification",
                    scope.breakdown_clarification(metric),
                    scope.breakdown_suggestions(catalog.year_max),
                    pending=PendingClarification("breakdown", text, partial),
                )
            # UNKNOWN with no reference to earlier context.
            short = text.strip().lower()
            if scope.is_affirmative(short) or scope.is_negative(short):
                # "yes" / "no" with nothing pending - not out of scope, just nothing to act on.
                return await self._respond(
                    turn, "conversational", scope.NOTHING_PENDING_REPLY,
                    scope.example_questions(catalog.year_max),
                )
            if allow_rewrite:
                rewritten = await self._try_rewrite(turn, text)
                if rewritten is not None:
                    return rewritten
            return await self._respond(
                turn,
                "conversational",
                scope.out_of_scope_reply(turn.year_range),
                scope.example_questions(catalog.year_max),
            )

        mapping = await self._map(turn, text)
        state = turn.session.state
        resolved = resolve_with_session(mapping, signals, state)

        if not resolved.is_listing and resolved.metric_alias is None:
            if signals.references_previous and not turn.session.history:
                return await self._respond(turn, "clarification", NOTHING_TO_REFER_TO_REPLY)
            if allow_rewrite:
                rewritten = await self._try_rewrite(turn, text)
                if rewritten is not None:
                    return rewritten
            return await self._respond(
                turn,
                "clarification",
                scope.metric_clarification(_scope_phrase(resolved)),
                scope.METRIC_SUGGESTIONS,
                pending=PendingClarification("metric", text, resolved),
            )

        inherited = _inherited_slots(mapping, resolved)
        if inherited:
            await turn.steps.done(
                "context", "Using conversation context", "Carried over from earlier: " + ", ".join(inherited)
            )
        return await self._answer_query(turn, resolved)

    # ------------------------------------------------------ clarification flow

    async def _resolve_pending(
        self, turn: _Turn, classification: ClassificationResult, pending: PendingClarification
    ) -> AnswerResult | None:
        """Interprets this message as an answer to the previous clarifying question.
        Returns None when it clearly isn't one (a greeting, a capability question, or
        a message that contributes nothing) - the caller then treats it as new."""
        text = turn.question
        q = text.lower().strip()
        if classification.intent in (Intent.GREETING, Intent.SCHEMA_INFO):
            return None
        # A message naming something the data can't answer (profit, customers...) is a
        # new question, never an "answer" to be merged - merging would silently drop
        # the concept and answer a different question.
        if scope.detect_unsupported(text, turn.year_range) is not None:
            return None

        if pending.kind == "confirm_rewrite":
            if scope.is_affirmative(q) and pending.proposed_question:
                await turn.steps.done(
                    "context", "Using conversation context",
                    f'Running the suggested question: "{pending.proposed_question}"',
                )
                return await self._run_rewritten(turn, pending.proposed_question)
            if scope.is_negative(q):
                return await self._respond(
                    turn, "conversational", scope.DECLINED_REPLY, scope.example_questions(turn.catalog.year_max)
                )
            return None

        if scope.is_negative(q):
            return await self._respond(
                turn, "conversational", scope.DECLINED_REPLY, scope.example_questions(turn.catalog.year_max)
            )

        mapping = await self._map(turn, text)
        all_time = mapping.all_time or scope.is_all_time(q)
        contributed = any(
            (
                mapping.metric_intent, mapping.dimension_intent, mapping.compare_dimension,
                mapping.years, mapping.top_n, mapping.filters, all_time,
            )
        )
        takes_default = scope.accepts_default(q) or scope.is_affirmative(q) or _word_in("total", q)
        if not contributed and not takes_default:
            return None

        partial = pending.partial
        notes = list(partial.notes) + [n for n in mapping.notes if n not in partial.notes]
        metric = mapping.metric_intent or partial.metric_alias
        if metric is None:
            # Asked once already - don't ask again; use the most common measure and say so.
            metric = "total_revenue"
            notes.append("You didn't name a measure, so I used total revenue.")

        new_dim = mapping.compare_dimension or mapping.dimension_intent
        dimension = new_dim or partial.dimension
        extra = mapping.extra_dimension if new_dim else partial.extra_dimension

        if len(mapping.years) >= 2:
            year, years = None, mapping.years
        elif mapping.year is not None:
            year, years = mapping.year, ()
        elif all_time:
            year, years = None, ()
        else:
            year, years = partial.year, partial.years

        filters = {f.column: f for f in partial.filters}
        filters.update({f.column: f for f in mapping.filters})

        resolved = ResolvedQuery(
            metric_alias=metric,
            dimension=dimension,
            extra_dimension=extra if extra != dimension else None,
            filters=tuple(filters.values()),
            year=year,
            years=years,
            top_n=mapping.top_n or partial.top_n,
            notes=tuple(notes),
        )
        combined = scope.describe_query(
            resolved.metric_alias, resolved.dimension, resolved.extra_dimension, resolved.year,
            resolved.years, resolved.top_n, tuple(v for f in resolved.filters for v in f.values),
        )
        if all_time and year is None and not years:
            combined += " (all time)"
        await turn.steps.done(
            "context",
            "Using conversation context",
            f'Combined your reply with the earlier question "{pending.original_question}" -> {combined}',
        )
        return await self._answer_query(turn, resolved)

    async def _offer_reshape(self, turn: _Turn, text: str, unsupported: scope.UnsupportedConcept) -> AnswerResult:
        mapping = await self._map(turn, text)
        metric = mapping.metric_intent or unsupported.substitute_metric
        dimension = mapping.compare_dimension or mapping.dimension_intent
        proposed = scope.describe_query(
            metric, dimension, mapping.extra_dimension if dimension else None, mapping.year,
            mapping.years if len(mapping.years) >= 2 else (), mapping.top_n,
            tuple(v for f in mapping.filters for v in f.values),
        )
        await turn.steps.done(
            "scope", "Checking what the data can answer",
            f'No {unsupported.key} data available - closest answerable: "{proposed}"',
            status="warning",
        )
        return await self._respond(
            turn,
            "clarification",
            scope.reshape_offer(unsupported.explanation, proposed),
            scope.RESHAPE_SUGGESTIONS,
            pending=PendingClarification(
                "confirm_rewrite", text, ResolvedQuery(metric_alias=metric), proposed_question=proposed
            ),
        )

    async def _offer_year_in_range(
        self, turn: _Turn, resolved: ResolvedQuery, missing: list[int]
    ) -> AnswerResult:
        """Asking for a year the data doesn't cover gets an explanation and the nearest
        valid alternative - not a bare empty result."""
        c = turn.catalog
        valid = tuple(y for y in resolved.years if c.year_min <= y <= c.year_max)
        nearest = max(c.year_min, min(c.year_max, missing[0]))
        year, years = (None, valid) if len(valid) >= 2 else (valid[0] if valid else nearest, ())
        proposed = scope.describe_query(
            resolved.metric_alias, resolved.dimension, resolved.extra_dimension, year, years,
            resolved.top_n, tuple(v for f in resolved.filters for v in f.values),
        )
        missing_text = " and ".join(str(y) for y in missing)
        await turn.steps.done(
            "scope", "Checking what the data can answer",
            f"No data for {missing_text} (available: {c.year_min}-{c.year_max})", status="warning",
        )
        return await self._respond(
            turn,
            "clarification",
            f"I only have sales data from {c.year_min} to {c.year_max}, so there's nothing for "
            f"{missing_text}. Would you like **{proposed}** instead?",
            [f"Yes, show {year or ' and '.join(map(str, years))}", "No thanks"],
            pending=PendingClarification(
                "confirm_rewrite", turn.question, replace(resolved, notes=()), proposed_question=proposed
            ),
        )

    # ------------------------------------------------------ LLM NLU fallback

    async def _try_rewrite(self, turn: _Turn, text: str) -> AnswerResult | None:
        if self._rewriter is None:
            return None
        await turn.steps.start("rewrite", "Interpreting your wording", "Asking the language model to rephrase")
        t0 = time.monotonic()
        result = await self._rewriter.rewrite(text, turn.session.history, turn.year_range)
        turn.classify_ms += int((time.monotonic() - t0) * 1000)
        if result is None:
            await turn.steps.done("rewrite", "Interpreting your wording", "Language model unavailable", status="warning")
            return None
        if not result.in_scope or not result.rewritten:
            await turn.steps.done("rewrite", "Interpreting your wording", "Outside the sales data", status="warning")
            return await self._respond(
                turn,
                "conversational",
                scope.out_of_scope_reply(turn.year_range),
                scope.example_questions(turn.catalog.year_max),
            )
        await turn.steps.done("rewrite", "Interpreting your wording", f'Rephrased as "{result.rewritten}"')
        return await self._run_rewritten(turn, result.rewritten)

    async def _run_rewritten(self, turn: _Turn, rewritten: str) -> AnswerResult:
        classification = await self._llm.classify_input(rewritten)
        result = await self._route(turn, rewritten, classification, allow_rewrite=False)
        return replace(result, interpreted_as=result.interpreted_as or rewritten)

    # --------------------------------------------------------------- replies

    async def _respond(
        self,
        turn: _Turn,
        response_type: str,
        text: str,
        suggestions: list[str] | None = None,
        pending: PendingClarification | None = None,
    ) -> AnswerResult:
        """Non-query replies keep the analytical state untouched (a greeting doesn't
        change what the conversation is about) - only history and `pending` change."""
        suggestions = list(suggestions or [])
        await turn.steps.done("reply", "Writing the reply")
        await self._session_store.update_session(
            turn.session_id,
            replace(turn.session.state, pending=pending),
            ConversationTurn(
                question=turn.question, sql=None, row_count=None, narrative_text=text,
                response_type=response_type, suggestions=tuple(suggestions),
            ),
        )
        return AnswerResult(
            response_type=response_type,
            narrative_text=text,
            formatting=DEFAULT_FORMATTING,
            metrics=_zeroed_metrics(turn.classify_ms),
            suggestions=suggestions,
        )

    async def _answer_query(self, turn: _Turn, resolved: ResolvedQuery) -> AnswerResult:
        catalog, steps, root_span = turn.catalog, turn.steps, turn.root_span
        out_of_range = _years_outside_data(resolved, catalog)
        if out_of_range:
            return await self._offer_year_in_range(turn, resolved, out_of_range)
        resolved = replace(
            resolved,
            series_limit=series_limit_for(resolved.dimension, resolved.extra_dimension, resolved.top_n),
            period_label=turn.year_range if resolved.year is None and not resolved.years else None,
        )
        plan = (
            f"List distinct {resolved.listing_dimension or 'items'}"
            if resolved.is_listing
            else scope.describe_query(
                resolved.metric_alias, resolved.dimension, resolved.extra_dimension, resolved.year,
                resolved.years, resolved.top_n, tuple(v for f in resolved.filters for v in f.values),
            )
        )
        if not resolved.is_listing and resolved.period_label:
            plan += f" ({resolved.period_label})"
        await steps.done("plan", "Planning the analysis", plan)

        t_total_start = time.monotonic()
        error_feedback: str | None = None
        attempts: list[str] = []
        llm_ms = turn.classify_ms
        guardrail_ms = sql_ms = 0

        for attempt in range(turn.max_retries + 1):
            label_suffix = f" (attempt {attempt + 1})" if attempt else ""
            await steps.start("sql", "Writing SQL" + label_suffix)
            with _tracer.start_as_current_span("llm.generate_sql") as span:
                span.set_attribute("copilot.attempt", attempt + 1)
                t0 = time.monotonic()
                raw_sql = await self._llm.generate_sql(turn.question, catalog, resolved, error_feedback)
                llm_ms += int((time.monotonic() - t0) * 1000)
                span.set_attribute("copilot.raw_sql", raw_sql)
            await steps.done("sql", "Writing SQL" + label_suffix)

            await steps.start("guardrail", "Checking query safety")
            with _tracer.start_as_current_span("guardrail.validate") as span:
                t0 = time.monotonic()
                try:
                    safe_sql = validate_and_reserialize_sql(raw_sql)
                except GuardrailViolation as exc:
                    guardrail_ms += int((time.monotonic() - t0) * 1000)
                    span.set_status(Status(StatusCode.ERROR, str(exc)))
                    attempts.append(f"attempt {attempt + 1}: guardrail rejected - {exc}")
                    error_feedback = str(exc)
                    await steps.done("guardrail", "Checking query safety", f"Rejected: {exc}", status="error")
                    continue
                guardrail_ms += int((time.monotonic() - t0) * 1000)
                span.set_attribute("copilot.sql", safe_sql)
            await steps.done(
                "guardrail", "Checking query safety", "Read-only SELECT on approved views, row limit enforced"
            )

            await steps.start("execute", "Running the query")
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
                    await steps.done("execute", "Running the query", "Database error - retrying", status="error")
                    continue
                sql_ms += int((time.monotonic() - t0) * 1000)
                span.set_attribute("copilot.row_count", len(rows))
            await steps.done("execute", "Running the query", f"{len(rows)} row{'s' if len(rows) != 1 else ''} returned")

            await steps.start("answer", "Summarizing the results")
            narrative = build_narrative(resolved, columns, rows)
            visualization = build_visualization(columns, rows)
            suggestions = (
                []
                if resolved.is_listing
                else scope.followup_suggestions(
                    resolved.metric_alias, resolved.dimension, resolved.extra_dimension,
                    resolved.year, resolved.years, resolved.top_n, catalog.year_max, catalog.year_min,
                )
            )
            await steps.done("answer", "Summarizing the results", "Numbers computed directly from the query result")

            total_ms = int((time.monotonic() - t_total_start) * 1000) + turn.classify_ms
            root_span.set_attribute("copilot.row_count", len(rows))
            root_span.set_attribute("copilot.attempts_used", attempt + 1)

            await self._update_state_after_query(
                turn, resolved, safe_sql, len(rows), narrative.text, visualization, suggestions
            )
            return AnswerResult(
                response_type="query",
                narrative_text=narrative.text,
                formatting=narrative.formatting,
                sql_executed=safe_sql,
                row_count=len(rows),
                is_truncated=len(rows) >= MAX_ROW_LIMIT,
                visualization=visualization,
                metrics={"llm_ms": llm_ms, "guardrail_ms": guardrail_ms, "sql_ms": sql_ms, "total_ms": total_ms},
                suggestions=suggestions,
                interpreted_as=plan,
            )

        root_span.set_attribute("copilot.failed_attempts", attempts)
        root_span.set_status(Status(StatusCode.ERROR, "exhausted retries"))
        raise OrchestratorError(
            f"Failed to produce a valid, executable query after {turn.max_retries + 1} attempts.",
            attempts=attempts,
        )

    async def _run_sql(self, sql: str) -> tuple[list[dict], list[str]]:
        """The `run_sql` tool: only ever called with guardrail-validated SQL."""
        async with self._pool.acquire() as conn:
            records = await conn.fetch(sql)
        rows = [dict(r) for r in records]
        columns = list(records[0].keys()) if records else []
        return rows, columns

    async def _update_state_after_query(
        self,
        turn: _Turn,
        resolved: ResolvedQuery,
        sql: str,
        row_count: int,
        narrative_text: str,
        visualization: VisualizationConfig,
        suggestions: list[str],
    ) -> None:
        state = turn.session.state
        if resolved.is_listing:
            # A listing detour doesn't change what the analytical thread is about.
            new_state = replace(state, pending=None)
        else:
            new_state = SessionState(
                last_metric_alias=resolved.metric_alias,
                last_dimension=resolved.dimension,
                last_extra_dimension=resolved.extra_dimension,
                last_year=resolved.year,
                active_filters={f.column: f for f in resolved.filters},
                output_preference=state.output_preference,
                pending=None,
            )
        await self._session_store.update_session(
            turn.session_id,
            new_state,
            ConversationTurn(
                question=turn.question,
                sql=sql,
                row_count=row_count,
                narrative_text=narrative_text,
                response_type="query",
                visualization=visualization.to_dict() if visualization else None,
                suggestions=tuple(suggestions),
            ),
        )


def _should_attempt_resolution(classification: ClassificationResult) -> bool:
    """A bare metric fragment ("revenue") must always ask how to slice it, never
    silently resolve to an all-time total from leftover session state. Resolution is
    attempted for genuine analytical questions, or for ambiguous messages that
    explicitly point at prior context (references_previous)."""
    if classification.intent == Intent.DATABASE_QUERY:
        return True
    return classification.intent in (Intent.CLARIFICATION, Intent.UNKNOWN) and (
        classification.extracted_signals.references_previous
    )


def _years_outside_data(resolved: ResolvedQuery, catalog: SchemaCatalog) -> list[int]:
    if resolved.is_listing or catalog.year_min is None or catalog.year_max is None:
        return []
    asked = ([resolved.year] if resolved.year is not None else []) + list(resolved.years)
    return [y for y in asked if not (catalog.year_min <= y <= catalog.year_max)]


def _scope_phrase(resolved: ResolvedQuery) -> str:
    parts: list[str] = []
    if resolved.dimension:
        dims = scope.dimension_label(resolved.dimension)
        if resolved.extra_dimension:
            dims += f" and {scope.dimension_label(resolved.extra_dimension)}"
        parts.append(f"each {dims}")
    values = [v for f in resolved.filters for v in f.values]
    if values:
        parts.append(" and ".join(values))
    if len(resolved.years) >= 2:
        parts.append(" and ".join(str(y) for y in resolved.years))
    elif resolved.year is not None:
        parts.append(str(resolved.year))
    return ", ".join(parts)


def _inherited_slots(mapping: TermMappingResult, resolved: ResolvedQuery) -> list[str]:
    out: list[str] = []
    if mapping.metric_intent is None and resolved.metric_alias:
        out.append(scope.metric_label(resolved.metric_alias))
    if not (mapping.dimension_intent or mapping.compare_dimension) and resolved.dimension:
        dims = scope.dimension_label(resolved.dimension)
        if resolved.extra_dimension:
            dims += f" x {scope.dimension_label(resolved.extra_dimension)}"
        out.append(f"by {dims}")
    if mapping.year is None and not mapping.years and resolved.year is not None:
        out.append(str(resolved.year))
    mapped_cols = {f.column for f in mapping.filters}
    for f in resolved.filters:
        if f.column not in mapped_cols:
            out.append(", ".join(f.values))
    return out


def _zeroed_metrics(classify_ms: int) -> dict[str, int]:
    return {"llm_ms": classify_ms, "guardrail_ms": 0, "sql_ms": 0, "total_ms": classify_ms}
