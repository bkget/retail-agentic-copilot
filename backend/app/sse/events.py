"""SSE event frame contract - see the project README for the full sequence. Each frame is
`event: <name>\\ndata: <json>\\n\\n`. `format_sse` is the only place that touches wire
format, so the contract can't drift between events emitted from different call sites.
"""

from __future__ import annotations

import json
from typing import Any

SCHEMA_VERSION = "2.1"


def format_sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def status_event(stage: str) -> str:
    return format_sse("status", {"stage": stage})


def sql_event(sql_executed: str, is_truncated: bool) -> str:
    return format_sse("sql", {"sql_executed": sql_executed, "is_truncated": is_truncated})


def narrative_delta_event(delta: str) -> str:
    return format_sse("narrative_delta", {"delta": delta})


def visualization_event(visualization: dict[str, Any]) -> str:
    return format_sse("visualization", visualization)


def metadata_event(metadata: dict[str, Any]) -> str:
    return format_sse("metadata", metadata)


def error_event(message: str) -> str:
    return format_sse("error", {"message": message})


def done_event(consolidated: dict[str, Any]) -> str:
    """Final frame carrying the fully consolidated response (schema section 6) - lets a
    client that doesn't want to assemble state from individual frames just use this one."""
    return format_sse("done", consolidated)


def step_event(step: dict[str, Any]) -> str:
    """One entry of the live reasoning trace ({id, label, detail, status, elapsed_ms}).
    Emitted with status "running" when a stage starts and again when it finishes - the
    client upserts by id, which is what drives the "Thinking..." panel."""
    return format_sse("step", step)


def suggestions_event(items: list[str], response_type: str) -> str:
    return format_sse("suggestions", {"items": items, "response_type": response_type})


def heartbeat_comment() -> str:
    """SSE comment line - keeps proxies from closing an idle stream; ignored by parsers."""
    return ": heartbeat\n\n"
