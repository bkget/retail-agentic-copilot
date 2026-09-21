"""In-memory session store: session_id -> recent turn history + resolved conversation
state, so follow-up questions ("same as before but by district", "only for 2024") can
be answered without re-explaining the whole request.

Deliberately a process-local dict, not Redis - correct for a single-instance portfolio
deployment, and explicitly NOT correct for multi-instance production (a second backend
replica would have no idea about a session's history). That's a known, intentional
scope boundary: swap this for a Redis-backed store first if this project ever needed to
run more than one backend replica.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from app.agent.llm_provider import ConversationTurn, SessionState


@dataclass(frozen=True)
class SessionContext:
    history: list[ConversationTurn]
    state: SessionState


@dataclass
class _SessionEntry:
    turns: list[ConversationTurn] = field(default_factory=list)
    state: SessionState = field(default_factory=SessionState)
    last_access: float = field(default_factory=time.monotonic)


class SessionStore:
    def __init__(self, ttl_seconds: int, max_turns: int):
        self._ttl_seconds = ttl_seconds
        self._max_turns = max_turns
        self._sessions: dict[str, _SessionEntry] = {}

    def get_session(self, session_id: str) -> SessionContext:
        self._evict_expired()
        entry = self._sessions.get(session_id)
        if entry is None:
            return SessionContext(history=[], state=SessionState())
        entry.last_access = time.monotonic()
        return SessionContext(history=list(entry.turns), state=entry.state)

    def update_session(
        self,
        session_id: str,
        state: SessionState,
        append_turn: ConversationTurn | None = None,
    ) -> None:
        """Overwrites the session's state and, when given a turn, appends it to
        history, capped at the last `max_turns` (default 10 per the "maintain chat
        history for the last 10 turns" requirement). Called once per request, at the
        end of Orchestrator.answer - state and history move together so a client can
        never observe one updated without the other."""
        entry = self._sessions.setdefault(session_id, _SessionEntry())
        entry.state = state
        if append_turn is not None:
            entry.turns.append(append_turn)
            if len(entry.turns) > self._max_turns:
                entry.turns = entry.turns[-self._max_turns :]
        entry.last_access = time.monotonic()

    def _evict_expired(self) -> None:
        now = time.monotonic()
        expired = [
            sid for sid, e in self._sessions.items() if now - e.last_access > self._ttl_seconds
        ]
        for sid in expired:
            del self._sessions[sid]
