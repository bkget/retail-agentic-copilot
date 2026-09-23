from __future__ import annotations

import asyncio
import logging
import re
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from opentelemetry.trace import Status, StatusCode
from pydantic import BaseModel, Field, field_validator

from app.agent import scope
from app.agent.llm_provider import LLMProvider, MockLLMProvider
from app.agent.nlu_fallback import create_rewriter
from app.agent.orchestrator import Orchestrator, OrchestratorError
from app.config import get_settings
from app.db.pool import create_agent_pool, create_refresher_pool
from app.observability.tracing import configure_tracing, current_trace_id, get_tracer
from app.ratelimit import TokenBucketRateLimiter
from app.refresh.scheduler import RefreshScheduler
from app.schema.catalog import load_catalog
from app.session.store import create_session_store
from app.sse.events import (
    SCHEMA_VERSION,
    done_event,
    error_event,
    heartbeat_comment,
    metadata_event,
    narrative_delta_event,
    sql_event,
    status_event,
    step_event,
    suggestions_event,
    visualization_event,
)

logger = logging.getLogger("copilot")

HEARTBEAT_INTERVAL_SECONDS = 8
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,100}$")


def _build_llm_provider(provider_name: str) -> LLMProvider:
    if provider_name == "mock":
        return MockLLMProvider()
    if provider_name == "gemini":
        # Deferred import: google-adk is optional and only needed when selected.
        try:
            from app.agent.gemini_provider import GeminiADKProvider
        except ImportError as exc:  # pragma: no cover - deployment misconfiguration
            raise RuntimeError(
                "LLM_PROVIDER=gemini needs the 'gemini' extra: rebuild with BACKEND_EXTRAS=redis,gemini"
            ) from exc

        return GeminiADKProvider()
    raise ValueError(f"Unknown LLM_PROVIDER '{provider_name}'")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format='{"ts":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}',
    )
    configure_tracing()
    app.state.settings = settings
    app.state.agent_pool = await create_agent_pool(settings)
    app.state.catalog = await load_catalog(app.state.agent_pool)
    app.state.llm_provider = _build_llm_provider(settings.llm_provider)
    app.state.session_store = create_session_store(settings)
    app.state.rewriter = create_rewriter(settings)
    app.state.orchestrator = Orchestrator(
        app.state.llm_provider, app.state.agent_pool, app.state.session_store, app.state.rewriter
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
    logger.info(
        "started provider=%s nlu_fallback=%s session_backend=%s",
        settings.llm_provider, settings.nlu_fallback, settings.session_backend,
    )

    yield

    app.state.refresh_scheduler.shutdown()
    if app.state.rewriter is not None:
        await app.state.rewriter.close()
    await app.state.session_store.close()
    await app.state.refresher_pool.close()
    await app.state.agent_pool.close()


app = FastAPI(title="Retail Agentic Copilot", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_allow_origins,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Content-Type"],
)


@app.get("/livez")
async def livez():
    return {"status": "ok"}


@app.get("/healthz")
async def healthz(request: Request):
    """Readiness: the process is up AND the database answers."""
    try:
        async with request.app.state.agent_pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
    except Exception:
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    return {"status": "ok"}


@app.get("/api/capabilities")
async def capabilities(request: Request):
    """What the UI shows on an empty chat: the data range and example questions."""
    catalog = request.app.state.catalog
    settings = request.app.state.settings
    return {
        "year_min": catalog.year_min,
        "year_max": catalog.year_max,
        "examples": scope.example_questions(catalog.year_max),
        "llm_provider": settings.llm_provider,
        "nlu_fallback": settings.nlu_fallback,
    }


def _validate_session_id(session_id: str) -> str | None:
    return session_id if SESSION_ID_RE.match(session_id) else None


@app.get("/api/session/{session_id}/history")
async def session_history(session_id: str, request: Request):
    """Rehydrates a returning session's chat (e.g. after a page reload)."""
    if not _validate_session_id(session_id):
        return JSONResponse(status_code=400, content={"error": "Invalid session id."})
    session = await request.app.state.session_store.get_session(session_id)
    return {
        "turns": [
            {
                "question": t.question,
                "sql": t.sql,
                "row_count": t.row_count,
                "narrative": t.narrative_text,
                "response_type": t.response_type or ("query" if t.sql else "conversational"),
                "visualization": t.visualization,
                "suggestions": list(t.suggestions),
            }
            for t in session.history
        ]
    }


@app.delete("/api/session/{session_id}")
async def clear_session(session_id: str, request: Request):
    if not _validate_session_id(session_id):
        return JSONResponse(status_code=400, content={"error": "Invalid session id."})
    await request.app.state.session_store.clear_session(session_id)
    return {"status": "cleared"}


class QueryRequest(BaseModel):
    question: str = Field(min_length=1)
    session_id: str = "anonymous"

    @field_validator("question")
    @classmethod
    def _question_ok(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("question must not be empty")
        if len(v) > get_settings().max_question_length:
            raise ValueError(f"question must be at most {get_settings().max_question_length} characters")
        return v

    @field_validator("session_id")
    @classmethod
    def _session_ok(cls, v: str) -> str:
        if not SESSION_ID_RE.match(v):
            raise ValueError("invalid session_id")
        return v


def _client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _narrative_chunks(text: str, words_per_chunk: int = 3) -> list[str]:
    tokens = re.findall(r"\S+\s*", text)
    return ["".join(tokens[i : i + words_per_chunk]) for i in range(0, len(tokens), words_per_chunk)]


@app.post("/api/query")
async def query(payload: QueryRequest, request: Request):
    app_state = request.app.state
    settings = app_state.settings

    if not app_state.rate_limiter.allow(_client_key(request)):
        return JSONResponse(
            status_code=429,
            content={"error": "You're sending questions quickly - please wait a few seconds and try again."},
        )

    async def stream():
        tracer = get_tracer()
        with tracer.start_as_current_span("api.query") as root_span:
            root_span.set_attribute("copilot.session_id", payload.session_id)
            trace_id = current_trace_id() or "unknown"

            yield status_event("thinking")

            catalog = app_state.catalog
            if catalog.is_stale():
                catalog = await load_catalog(app_state.agent_pool)
                app_state.catalog = catalog

            steps: asyncio.Queue[dict] = asyncio.Queue()

            async def on_step(step: dict) -> None:
                await steps.put(step)

            task = asyncio.create_task(
                app_state.orchestrator.answer(
                    payload.question,
                    catalog,
                    payload.session_id,
                    max_retries=settings.max_generation_retries,
                    on_step=on_step,
                )
            )
            try:
                # Relay reasoning steps live while the turn runs. A heartbeat comment
                # keeps browsers/proxies from treating a slow LLM call as a dead stream.
                getter: asyncio.Future | None = None
                while True:
                    if getter is None:
                        getter = asyncio.ensure_future(steps.get())
                    done, _ = await asyncio.wait(
                        {getter, task}, timeout=HEARTBEAT_INTERVAL_SECONDS,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if getter in done:
                        yield step_event(getter.result())
                        getter = None
                        continue
                    if task in done:
                        getter.cancel()
                        while not steps.empty():
                            yield step_event(steps.get_nowait())
                        break
                    yield heartbeat_comment()

                try:
                    result = task.result()
                except OrchestratorError as exc:
                    root_span.record_exception(exc)
                    yield error_event(
                        "I couldn't build a valid query for that question. Could you rephrase it - "
                        "for example, 'total revenue by division in 2020'?"
                    )
                    return
                except Exception as exc:
                    # Provider failures (quota, network, safety filter) - logged with the
                    # trace id, never leaked verbatim to the client.
                    root_span.record_exception(exc)
                    root_span.set_status(Status(StatusCode.ERROR, "unhandled provider error"))
                    logger.exception("query failed trace_id=%s", trace_id)
                    yield error_event(
                        "The AI provider is temporarily unavailable or over its usage limit. "
                        "Please try again in a moment."
                    )
                    return

                is_query = result.response_type == "query"
                if is_query:
                    yield sql_event(result.sql_executed, result.is_truncated)

                yield status_event("summarizing")
                delay = settings.narrative_stream_delay_ms / 1000
                for chunk in _narrative_chunks(result.narrative_text):
                    yield narrative_delta_event(chunk)
                    if delay:
                        await asyncio.sleep(delay)

                if is_query and result.visualization is not None:
                    yield visualization_event(result.visualization.to_dict())

                if result.suggestions:
                    yield suggestions_event(result.suggestions, result.response_type)

                last_refreshed_at = None
                if is_query:
                    async with app_state.agent_pool.acquire() as conn:
                        refresh_rows = await conn.fetch("SELECT refreshed_at FROM public.refresh_log")
                    last_refreshed_at = max((r["refreshed_at"] for r in refresh_rows), default=None)
                refreshed_iso = last_refreshed_at.isoformat() if last_refreshed_at else None

                yield metadata_event(
                    {
                        **result.metrics,
                        "row_count": result.row_count,
                        "is_truncated": result.is_truncated,
                        "trace_id": trace_id,
                        "last_refreshed_at": refreshed_iso,
                        "response_type": result.response_type,
                        "interpreted_as": result.interpreted_as,
                    }
                )
                yield done_event(
                    {
                        "schema_version": SCHEMA_VERSION,
                        "trace_id": trace_id,
                        "status": "success",
                        "response_type": result.response_type,
                        "metrics": result.metrics,
                        "last_refreshed_at": refreshed_iso,
                        "data_summary": {
                            "row_count": result.row_count,
                            "is_truncated": result.is_truncated,
                            "formatting": result.formatting,
                        },
                        "narrative": result.narrative_text,
                        "visualization": result.visualization.to_dict() if result.visualization else None,
                        "suggestions": result.suggestions,
                        "interpreted_as": result.interpreted_as,
                    }
                )
            finally:
                # Client disconnected / pressed Stop: don't keep burning LLM or DB time.
                if not task.done():
                    task.cancel()

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
