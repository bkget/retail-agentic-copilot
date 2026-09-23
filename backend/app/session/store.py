"""Session store: session_id -> recent turn history + resolved conversation state, so
follow-ups ("same as before but by district", "only for 2024") and answers to a
clarifying question ("2020", "all time") resolve against what was said before.

Two interchangeable backends behind one async interface:
  * InMemorySessionStore (default) - process-local, correct for a single replica.
  * RedisSessionStore (SESSION_BACKEND=redis) - shared across replicas, TTL enforced
    by Redis itself. State is serialized to plain JSON (see _state_to_dict), never
    pickled, so a store written by one version can be inspected/migrated safely.

`SessionStore` is kept as an alias of the in-memory store for backwards compatibility.
"""

from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from collections import OrderedDict
from dataclasses import asdict, dataclass, field
from typing import Any

from app.agent.llm_provider import (
    ConversationTurn,
    PendingClarification,
    ResolvedQuery,
    SessionState,
    _NamedFilter,
)


@dataclass(frozen=True)
class SessionContext:
    history: list[ConversationTurn]
    state: SessionState


# ---------------------------------------------------------------------------
# JSON (de)serialization - explicit, so the wire format is stable and reviewable
# ---------------------------------------------------------------------------


def _filter_to_dict(f: _NamedFilter) -> dict[str, Any]:
    return {"column": f.column, "op": f.op, "values": list(f.values)}


def _filter_from_dict(d: dict[str, Any]) -> _NamedFilter:
    return _NamedFilter(d["column"], d["op"], tuple(d["values"]))


def _resolved_to_dict(r: ResolvedQuery) -> dict[str, Any]:
    d = asdict(r)
    d["filters"] = [_filter_to_dict(f) for f in r.filters]
    return d


def _resolved_from_dict(d: dict[str, Any]) -> ResolvedQuery:
    d = dict(d)
    d["filters"] = tuple(_filter_from_dict(f) for f in d.get("filters", []))
    d["years"] = tuple(d.get("years", ()))
    d["notes"] = tuple(d.get("notes", ()))
    known = ResolvedQuery.__dataclass_fields__.keys()
    return ResolvedQuery(**{k: v for k, v in d.items() if k in known})


def _state_to_dict(s: SessionState) -> dict[str, Any]:
    pending = None
    if s.pending is not None:
        pending = {
            "kind": s.pending.kind,
            "original_question": s.pending.original_question,
            "partial": _resolved_to_dict(s.pending.partial),
            "proposed_question": s.pending.proposed_question,
            "attempts": s.pending.attempts,
        }
    return {
        "last_metric_alias": s.last_metric_alias,
        "last_dimension": s.last_dimension,
        "last_extra_dimension": s.last_extra_dimension,
        "last_year": s.last_year,
        "active_filters": {k: _filter_to_dict(v) for k, v in s.active_filters.items()},
        "output_preference": s.output_preference,
        "pending": pending,
    }


def _state_from_dict(d: dict[str, Any]) -> SessionState:
    pending = None
    if d.get("pending"):
        p = d["pending"]
        pending = PendingClarification(
            kind=p["kind"],
            original_question=p["original_question"],
            partial=_resolved_from_dict(p["partial"]),
            proposed_question=p.get("proposed_question"),
            attempts=p.get("attempts", 1),
        )
    return SessionState(
        last_metric_alias=d.get("last_metric_alias"),
        last_dimension=d.get("last_dimension"),
        last_extra_dimension=d.get("last_extra_dimension"),
        last_year=d.get("last_year"),
        active_filters={k: _filter_from_dict(v) for k, v in d.get("active_filters", {}).items()},
        output_preference=d.get("output_preference", "grouped"),
        pending=pending,
    )


def _turn_to_dict(t: ConversationTurn) -> dict[str, Any]:
    return asdict(t)


def _turn_from_dict(d: dict[str, Any]) -> ConversationTurn:
    known = ConversationTurn.__dataclass_fields__.keys()
    data = {k: v for k, v in d.items() if k in known}
    data["suggestions"] = tuple(data.get("suggestions") or ())
    return ConversationTurn(**data)


# ---------------------------------------------------------------------------
# Interface
# ---------------------------------------------------------------------------


class BaseSessionStore(ABC):
    @abstractmethod
    async def get_session(self, session_id: str) -> SessionContext: ...

    @abstractmethod
    async def update_session(
        self, session_id: str, state: SessionState, append_turn: ConversationTurn | None = None
    ) -> None:
        """Overwrites state and (optionally) appends a turn, capped at max_turns. Called
        once per request so state and history always move together."""

    @abstractmethod
    async def clear_session(self, session_id: str) -> None: ...

    async def close(self) -> None:  # pragma: no cover - trivial
        return None


@dataclass
class _SessionEntry:
    turns: list[ConversationTurn] = field(default_factory=list)
    state: SessionState = field(default_factory=SessionState)
    last_access: float = field(default_factory=time.monotonic)


class InMemorySessionStore(BaseSessionStore):
    """LRU-bounded (max_sessions) so an unauthenticated client minting random session
    ids can't grow memory without limit; idle sessions expire after ttl_seconds."""

    def __init__(self, ttl_seconds: int, max_turns: int, max_sessions: int = 10_000):
        self._ttl_seconds = ttl_seconds
        self._max_turns = max_turns
        self._max_sessions = max_sessions
        self._sessions: OrderedDict[str, _SessionEntry] = OrderedDict()

    async def get_session(self, session_id: str) -> SessionContext:
        self._evict_expired()
        entry = self._sessions.get(session_id)
        if entry is None:
            return SessionContext(history=[], state=SessionState())
        entry.last_access = time.monotonic()
        self._sessions.move_to_end(session_id)
        return SessionContext(history=list(entry.turns), state=entry.state)

    async def update_session(
        self, session_id: str, state: SessionState, append_turn: ConversationTurn | None = None
    ) -> None:
        entry = self._sessions.get(session_id)
        if entry is None:
            entry = self._sessions[session_id] = _SessionEntry()
        entry.state = state
        if append_turn is not None:
            entry.turns.append(append_turn)
            if len(entry.turns) > self._max_turns:
                entry.turns = entry.turns[-self._max_turns :]
        entry.last_access = time.monotonic()
        self._sessions.move_to_end(session_id)
        while len(self._sessions) > self._max_sessions:
            self._sessions.popitem(last=False)

    async def clear_session(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    def _evict_expired(self) -> None:
        now = time.monotonic()
        expired = [sid for sid, e in self._sessions.items() if now - e.last_access > self._ttl_seconds]
        for sid in expired:
            del self._sessions[sid]


# Backwards-compatible name used by tests and the eval harness.
SessionStore = InMemorySessionStore


class RedisSessionStore(BaseSessionStore):
    _PREFIX = "copilot:session:"

    def __init__(self, redis_url: str, ttl_seconds: int, max_turns: int):
        import redis.asyncio as redis  # optional dependency, only needed for this backend

        self._redis = redis.from_url(redis_url, decode_responses=True)
        self._ttl_seconds = ttl_seconds
        self._max_turns = max_turns

    def _key(self, session_id: str) -> str:
        return f"{self._PREFIX}{session_id}"

    async def get_session(self, session_id: str) -> SessionContext:
        raw = await self._redis.get(self._key(session_id))
        if not raw:
            return SessionContext(history=[], state=SessionState())
        await self._redis.expire(self._key(session_id), self._ttl_seconds)
        data = json.loads(raw)
        return SessionContext(
            history=[_turn_from_dict(t) for t in data.get("turns", [])],
            state=_state_from_dict(data.get("state", {})),
        )

    async def update_session(
        self, session_id: str, state: SessionState, append_turn: ConversationTurn | None = None
    ) -> None:
        key = self._key(session_id)
        raw = await self._redis.get(key)
        turns = json.loads(raw).get("turns", []) if raw else []
        if append_turn is not None:
            turns.append(_turn_to_dict(append_turn))
            turns = turns[-self._max_turns :]
        payload = json.dumps({"turns": turns, "state": _state_to_dict(state)})
        await self._redis.set(key, payload, ex=self._ttl_seconds)

    async def clear_session(self, session_id: str) -> None:
        await self._redis.delete(self._key(session_id))

    async def close(self) -> None:
        await self._redis.aclose()


def create_session_store(settings) -> BaseSessionStore:
    if settings.session_backend == "redis":
        if not settings.redis_url:
            raise RuntimeError("SESSION_BACKEND=redis but REDIS_URL is not set")
        return RedisSessionStore(settings.redis_url, settings.session_ttl_seconds, settings.session_max_turns)
    return InMemorySessionStore(settings.session_ttl_seconds, settings.session_max_turns)
