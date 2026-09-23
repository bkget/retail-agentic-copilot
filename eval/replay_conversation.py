#!/usr/bin/env python3
"""Replays a multi-turn conversation against the running API over SSE, printing the
reasoning steps, reply type and answer for each turn - a quick end-to-end smoke test
of the exact wire protocol the browser uses.

    python eval/replay_conversation.py                       # built-in regression script
    python eval/replay_conversation.py "revenue" "2020"      # your own turns
"""

from __future__ import annotations

import json
import sys
import uuid

import httpx

API = "http://localhost:8000"
DEFAULT_TURNS = [
    "Compare the sales revenue per store and monthly distribution",
    "2020",
    "GO WITH ALL TIME",
    "what question kind you can answer?",
    "how to compare Stores per divisions, and districts on 2020",
    "Revenue",
    "What is the profit by division?",
    "yes",
    "who won the world cup?",
]


def main(turns: list[str]) -> int:
    session = f"replay-{uuid.uuid4().hex[:8]}"
    failures = 0
    with httpx.Client(timeout=60) as client:
        for q in turns:
            print(f"\n\033[1mYOU:\033[0m {q}")
            event, rtype, narrative, steps, suggestions, sql = None, None, "", [], [], None
            with client.stream("POST", f"{API}/api/query", json={"question": q, "session_id": session}) as r:
                for line in r.iter_lines():
                    if line.startswith("event: "):
                        event = line[7:]
                    elif line.startswith("data: "):
                        data = json.loads(line[6:])
                        if event == "step" and data["status"] != "running":
                            steps.append(f"{data['label']}: {data.get('detail') or ''}".rstrip(": "))
                        elif event == "narrative_delta":
                            narrative += data["delta"]
                        elif event == "sql":
                            sql = data["sql_executed"]
                        elif event == "suggestions":
                            suggestions = data["items"]
                        elif event == "done":
                            rtype = data["response_type"]
                        elif event == "error":
                            rtype, narrative = "error", data["message"]
            for s in steps:
                print(f"   \033[2m✓ {s}\033[0m")
            print(f"\033[1mASSISTANT [{rtype}]:\033[0m {narrative}")
            if sql:
                print(f"   \033[2mSQL: {sql}\033[0m")
            if suggestions:
                print(f"   \033[2mchips: {suggestions}\033[0m")
            if rtype in (None, "error"):
                failures += 1
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:] or DEFAULT_TURNS))
