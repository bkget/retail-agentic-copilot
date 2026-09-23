"""Pure unit tests (no database): session-state serialization used by the Redis
backend, scope helpers, and the LLM-fallback reply parser."""

from __future__ import annotations

from app.agent import scope
from app.agent.llm_provider import (
    ConversationTurn,
    PendingClarification,
    ResolvedQuery,
    SessionState,
    _NamedFilter,
)
from app.agent.nlu_fallback import parse_rewrite_reply
from app.session.store import (
    InMemorySessionStore,
    _state_from_dict,
    _state_to_dict,
    _turn_from_dict,
    _turn_to_dict,
)


def test_session_state_round_trips_through_json_including_pending():
    f = _NamedFilter("store_division", "=", ("DHAKA",))
    state = SessionState(
        last_metric_alias="total_revenue",
        last_dimension="store_district",
        last_extra_dimension="sale_month",
        last_year=2020,
        active_filters={"store_division": f},
        pending=PendingClarification(
            "metric", "only for 2024", ResolvedQuery(year=2024, filters=(f,), notes=("n",))
        ),
    )
    import json

    restored = _state_from_dict(json.loads(json.dumps(_state_to_dict(state))))
    assert restored == state


def test_turn_round_trip():
    import json

    t = ConversationTurn("q", "SELECT 1", 1, "text", "query", {"a": 1}, ("x",))
    assert _turn_from_dict(json.loads(json.dumps(_turn_to_dict(t)))) == t


async def test_in_memory_store_is_lru_bounded():
    store = InMemorySessionStore(ttl_seconds=60, max_turns=3, max_sessions=2)
    for sid in ("a", "b", "c"):
        await store.update_session(sid, SessionState())
    assert (await store.get_session("a")).history == []
    assert len(store._sessions) == 2


def test_all_time_and_short_reply_detection():
    assert scope.is_all_time("GO WITH ALL TIME")
    assert scope.is_all_time("overall please")
    assert not scope.is_all_time("2020")
    assert scope.is_affirmative("Yes, show that")
    assert scope.is_negative("no thanks")
    assert not scope.is_affirmative("yesterday's revenue by division in 2020 please thanks")


def test_unsupported_concepts_are_detected_with_whole_words():
    assert scope.detect_unsupported("profit by division").key == "profit"
    assert scope.detect_unsupported("top customers").key == "customer"
    assert scope.detect_unsupported("total revenue by division") is None


def test_describe_query_is_reparseable_text():
    assert scope.describe_query("total_revenue", "store_district", "sale_month", 2020) == (
        "Total revenue by district and month in 2020"
    )
    assert scope.describe_query("total_units", "item_name", top_n=5) == "Top 5 items by units sold"


def test_parse_rewrite_reply_accepts_json_with_surrounding_prose():
    r = parse_rewrite_reply('Sure! {"in_scope": true, "rewritten": "Total revenue by division", "reason": "ok"}')
    assert r.in_scope and r.rewritten == "Total revenue by division"


def test_parse_rewrite_reply_rejects_garbage():
    assert parse_rewrite_reply("I think revenue") is None
    r = parse_rewrite_reply('{"in_scope": true, "rewritten": null}')
    assert r is not None and r.in_scope is False
