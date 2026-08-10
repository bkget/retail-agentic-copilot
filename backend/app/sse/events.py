"""SSE event frame contract - see the project README for the full sequence. Each frame is
`event: <name>\\ndata: <json>\\n\\n`. `format_sse` is the only place that touches wire
format, so the contract can't drift between events emitted from different call sites.
"""

from __future__ import annotations

import json
from typing import Any

SCHEMA_VERSION = "2.0"


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
