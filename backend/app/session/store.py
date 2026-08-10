"""Minimal in-memory session store: session_id -> recent turn history, so follow-up
questions ("now break that down by month") can reference prior context.

Deliberately a process-local dict, not Redis - correct for a single-instance portfolio
deployment, and explicitly NOT correct for multi-instance production (a second backend
replica would have no idea about a session's history). That's a known, intentional
scope boundary: swap this for a Redis-backed store first if this project ever needed to
run more than one backend replica.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from app.agent.llm_provider import ConversationTurn


@dataclass
class _SessionEntry:
    turns: list[ConversationTurn] = field(default_factory=list)
    last_access: float = field(default_factory=time.monotonic)


class SessionStore:
    def __init__(self, ttl_seconds: int, max_turns: int):
        self._ttl_seconds = ttl_seconds
        self._max_turns = max_turns
        self._sessions: dict[str, _SessionEntry] = {}

    def get_history(self, session_id: str) -> list[ConversationTurn]:
        self._evict_expired()
        entry = self._sessions.get(session_id)
        return list(entry.turns) if entry else []

    def append_turn(self, session_id: str, turn: ConversationTurn) -> None:
        entry = self._sessions.setdefault(session_id, _SessionEntry())
        entry.turns.append(turn)
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
