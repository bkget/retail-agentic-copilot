from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from opentelemetry.trace import Status, StatusCode
from pydantic import BaseModel

from app.agent.llm_provider import LLMProvider, MockLLMProvider
from app.agent.orchestrator import Orchestrator, OrchestratorError
from app.config import get_settings
from app.db.pool import create_agent_pool, create_refresher_pool
from app.observability.tracing import configure_tracing, current_trace_id, get_tracer
from app.ratelimit import TokenBucketRateLimiter
from app.refresh.scheduler import RefreshScheduler
from app.schema.catalog import load_catalog
from app.session.store import SessionStore
from app.sse.events import (
    SCHEMA_VERSION,
    done_event,
    error_event,
    metadata_event,
    narrative_delta_event,
    sql_event,
    status_event,
    visualization_event,
)


def _build_llm_provider(provider_name: str) -> LLMProvider:
    if provider_name == "mock":
        return MockLLMProvider()
    if provider_name == "gemini":
        # Deferred import: the gemini extra (google-adk) is optional and only needed
        # when actually selected, so `mock` mode has no hard dependency on it.
        from app.agent.gemini_provider import GeminiADKProvider

        return GeminiADKProvider()
    raise ValueError(f"Unknown LLM_PROVIDER '{provider_name}'")


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_tracing()
    settings = get_settings()
    app.state.settings = settings
    app.state.agent_pool = await create_agent_pool(settings)
    app.state.catalog = await load_catalog(app.state.agent_pool)
    app.state.llm_provider = _build_llm_provider(settings.llm_provider)
    app.state.session_store = SessionStore(settings.session_ttl_seconds, settings.session_max_turns)
    app.state.orchestrator = Orchestrator(
        app.state.llm_provider, app.state.agent_pool, app.state.session_store
    )
    app.state.rate_limiter = TokenBucketRateLimiter(
        settings.rate_limit_capacity, settings.rate_limit_refill_per_minute
    )

    app.state.refresher_pool = await create_refresher_pool(settings)

    async def _reload_catalog() -> None:
        app.state.catalog = await load_catalog(app.state.agent_pool)

    app.state.refresh_scheduler = RefreshScheduler(
        app.state.refresher_pool, settings.refresh_interval_minutes, on_refreshed=_reload_catalog
    )
    app.state.refresh_scheduler.start()

    yield

    app.state.refresh_scheduler.shutdown()
    await app.state.refresher_pool.close()
    await app.state.agent_pool.close()


HEARTBEAT_INTERVAL_SECONDS = 8

app = FastAPI(title="Enterprise Agentic Data Copilot", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_allow_origins,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


@app.get("/healthz")
async def healthz(request: Request):
    pool = request.app.state.agent_pool
    async with pool.acquire() as conn:
        await conn.fetchval("SELECT 1")
    return {"status": "ok"}


class QueryRequest(BaseModel):
    question: str
    session_id: str = "anonymous"


@app.get("/api/session/{session_id}/history")
async def session_history(session_id: str, request: Request):
    """Lets the frontend rehydrate a returning session's chat log on page load - the
    SessionStore already keeps this server-side (see app/session/store.py) for
    resolving follow-ups, but nothing previously exposed it for the UI to render.
    Only question/sql/row_count/narrative are persisted per turn (no chart config or
    per-request timing metrics), so a rehydrated turn shows its text but not its
    chart - a disclosed, minor fidelity gap, not a bug."""
    app_state = request.app.state
    session = app_state.session_store.get_session(session_id)
    return {
        "turns": [
            {
                "question": turn.question,
                "sql": turn.sql,
                "row_count": turn.row_count,
                "narrative": turn.narrative_text,
            }
            for turn in session.history
        ]
    }


@app.post("/api/query")
async def query(payload: QueryRequest, request: Request):
    app_state = request.app.state
    settings: object = app_state.settings

    if not app_state.rate_limiter.allow(payload.session_id):
        return JSONResponse(
            status_code=429,
            content={"error": "Rate limit exceeded. Please wait before asking another question."},
        )

    async def stream():
        tracer = get_tracer()
        with tracer.start_as_current_span("api.query") as root_span:
            root_span.set_attribute("copilot.session_id", payload.session_id)
            root_span.set_attribute("copilot.question", payload.question)
            trace_id = current_trace_id() or "unknown"

            yield status_event("parsing_intent")

            catalog = app_state.catalog
            if catalog.is_stale():
                catalog = await load_catalog(app_state.agent_pool)
                app_state.catalog = catalog

            # classify_input, get_session, map_terms_to_columns, generate_sql, run_sql
            # and update_session all happen inside orchestrator.answer() itself - a
            # single call now covers "is this even a query?" through execution and
            # session persistence, so there's no natural point between classify and
            # generate to narrate separately; "thinking" covers both.
            yield status_event("thinking")
            answer_task = asyncio.ensure_future(
                app_state.orchestrator.answer(
                    payload.question,
                    catalog,
                    payload.session_id,
                    max_retries=settings.max_generation_retries,
                )
            )
            try:
                # A real LLM provider can make several sequential model calls per turn
                # (classify, map terms, generate SQL - observed 15-40+ seconds total)
                # with nothing otherwise sent on the wire in that window. A long silent
                # gap on a held-open connection reads as dead to browsers, proxies, and
                # Docker's own port-forwarding layer - this produced a hard "network
                # error" on the client that had nothing to do with the model being
                # slow, only with the connection looking abandoned. A bare SSE comment
                # line is valid per the spec and silently ignored by any conformant
                # parser (this app's own sseClient.ts requires both an `event:` and a
                # `data:` line to treat something as a frame, so a comment-only line
                # never reaches the UI), so it keeps the connection alive with zero
                # visible effect.
                while not answer_task.done():
                    _, pending = await asyncio.wait({answer_task}, timeout=HEARTBEAT_INTERVAL_SECONDS)
                    if pending:
                        yield ": heartbeat\n\n"
                result = answer_task.result()
            except OrchestratorError as exc:
                yield error_event(str(exc))
                return
            except Exception as exc:
                # Anything the LLM provider itself raises directly - a real API call
                # can fail with a quota/rate-limit error, a transient network error, a
                # safety-filter block, etc. - isn't wrapped in OrchestratorError (that's
                # reserved for "generation ran, but produced no valid/executable SQL
                # after retries"). Without this, such an error propagated as an
                # unhandled exception mid-stream, abruptly closing the connection with
                # no clean error frame - a bare, uninformative "network error" on the
                # client for what was actually a clean, nameable failure the whole
                # time. Logged with the trace_id (not re-raised) so it's diagnosable
                # server-side without leaking provider-specific error text to the
                # client.
                root_span.record_exception(exc)
                root_span.set_status(Status(StatusCode.ERROR, "unhandled LLM provider error"))
                yield error_event(
                    "The AI provider is temporarily unavailable or over its usage "
                    "limit. Please try again in a moment."
                )
                return

            is_query = result.response_type == "query"

            if is_query:
                yield sql_event(result.sql_executed, result.is_truncated)

            yield status_event("summarizing")
            words = result.narrative_text.split(" ")
            for i in range(0, len(words), 4):
                chunk = " ".join(words[i : i + 4])
                yield narrative_delta_event(chunk + (" " if i + 4 < len(words) else ""))

            if is_query and result.visualization is not None:
                yield visualization_event(result.visualization.to_dict())

            last_refreshed_at = None
            if is_query:
                # Only meaningful when data was actually queried - skip the round-trip
                # entirely for conversational/clarification turns.
                async with app_state.agent_pool.acquire() as conn:
                    refresh_rows = await conn.fetch(
                        "SELECT view_name, refreshed_at FROM public.refresh_log"
                    )
                last_refreshed_at = max((r["refreshed_at"] for r in refresh_rows), default=None)

            metadata = {
                **result.metrics,
                "row_count": result.row_count,
                "is_truncated": result.is_truncated,
                "trace_id": trace_id,
                "last_refreshed_at": last_refreshed_at.isoformat() if last_refreshed_at else None,
                "response_type": result.response_type,
            }
            yield metadata_event(metadata)

            consolidated = {
                "schema_version": SCHEMA_VERSION,
                "trace_id": trace_id,
                "status": "success",
                "response_type": result.response_type,
                "metrics": result.metrics,
                "last_refreshed_at": last_refreshed_at.isoformat() if last_refreshed_at else None,
                "data_summary": {
                    "row_count": result.row_count,
                    "is_truncated": result.is_truncated,
                    "formatting": result.formatting,
                },
                "narrative": result.narrative_text,
                "visualization": result.visualization.to_dict() if result.visualization else None,
            }
            yield done_event(consolidated)

    return StreamingResponse(stream(), media_type="text/event-stream")
