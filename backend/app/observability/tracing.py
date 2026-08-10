"""OpenTelemetry setup. Defaults to a console exporter - enough to see real spans
(llm generation / guardrail validation / SQL execution) with real timings locally
without standing up a collector. Swapping to OTLP (Jaeger, Tempo, an APM vendor) is a
one-line change to the span processor/exporter in `configure_tracing`; nothing in
`app/agent/orchestrator.py` or elsewhere needs to change, since callers only ever use
`get_tracer()`.
"""

from __future__ import annotations

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from opentelemetry.trace import Tracer

SERVICE_NAME = "agentic-data-copilot-backend"

_configured = False


def configure_tracing() -> None:
    global _configured
    if _configured:
        return
    provider = TracerProvider(resource=Resource.create({"service.name": SERVICE_NAME}))
    provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
    trace.set_tracer_provider(provider)
    _configured = True


def get_tracer() -> Tracer:
    return trace.get_tracer(SERVICE_NAME)


def current_trace_id() -> str | None:
    """Returns the W3C-formatted (32 hex char) trace ID of the currently active span,
    or None if tracing hasn't been configured / there's no active span."""
    span_context = trace.get_current_span().get_span_context()
    if not span_context.is_valid:
        return None
    return trace.format_trace_id(span_context.trace_id)
