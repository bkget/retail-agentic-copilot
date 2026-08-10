#!/usr/bin/env python3
"""Execution Accuracy (EX) evaluation harness.

For each (question, ground_truth_sql) pair in golden_set.jsonl:
  1. Run the question through the real orchestrator (guardrail + execution included) to
     get the agent's generated SQL and result set.
  2. Independently guardrail-validate and execute the ground-truth SQL against the same
     live semantic layer.
  3. Compare the two result sets for equivalence (order-insensitive; each row's own
     column order must match, since ground truth is authored to mirror the agent's
     column ordering convention - dimension(s) first, then the metric).

This directly exercises app.security.ast_guardrail and app.agent.orchestrator - it is
not a mock of the pipeline, it runs the real thing against the real database.

Usage:
    python eval/eval_harness.py [--provider mock|gemini] [--threshold 0.8]

Exits non-zero if accuracy falls below --threshold, so this can gate CI (see
.github/workflows/eval.yml).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import asyncpg

BACKEND_ROOT = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_ROOT))

from app.agent.llm_provider import LLMProvider, MockLLMProvider  # noqa: E402
from app.agent.orchestrator import Orchestrator, OrchestratorError  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.schema.catalog import load_catalog  # noqa: E402
from app.security.ast_guardrail import GuardrailViolation, validate_and_reserialize_sql  # noqa: E402

GOLDEN_SET_PATH = Path(__file__).resolve().parent / "golden_set.jsonl"
FLOAT_TOLERANCE = 0.01


@dataclass
class EvalCase:
    id: str
    question: str
    ground_truth_sql: str


@dataclass
class EvalResult:
    case: EvalCase
    passed: bool
    reason: str
    generated_sql: str | None


def load_golden_set() -> list[EvalCase]:
    cases = []
    with open(GOLDEN_SET_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            cases.append(EvalCase(row["id"], row["question"], row["ground_truth_sql"]))
    return cases


def _normalize_row(row: asyncpg.Record) -> tuple:
    normalized = []
    for value in row.values():
        if isinstance(value, (float, Decimal)):
            # total_price/total_revenue are `numeric` in the semantic layer (see
            # db/init/04_semantic_views.sql) specifically so SUM() is exact and
            # deterministic - this rounding is purely to absorb harmless float
            # artifacts from AVG()/other non-additive numeric ops, not to paper over
            # real nondeterminism the way it would have had to before that fix.
            normalized.append(round(float(value), 2))
        else:
            normalized.append(value)
    return tuple(normalized)


def _result_sets_match(a: list[asyncpg.Record], b: list[asyncpg.Record]) -> bool:
    def as_multiset(records):
        counts: dict[tuple, int] = {}
        for r in records:
            key = _normalize_row(r)
            counts[key] = counts.get(key, 0) + 1
        return counts

    return as_multiset(a) == as_multiset(b)


async def _build_llm_provider(provider_name: str) -> LLMProvider:
    if provider_name == "mock":
        return MockLLMProvider()
    if provider_name == "gemini":
        from app.agent.gemini_provider import GeminiADKProvider

        return GeminiADKProvider()
    raise ValueError(f"Unknown provider '{provider_name}'")


async def run_eval(provider_name: str) -> list[EvalResult]:
    settings = get_settings()
    pool = await asyncpg.create_pool(dsn=settings.agent_dsn(), min_size=2, max_size=6)
    try:
        catalog = await load_catalog(pool)
        llm = await _build_llm_provider(provider_name)
        orchestrator = Orchestrator(llm, pool)

        cases = load_golden_set()
        results: list[EvalResult] = []

        for case in cases:
            try:
                truth_sql = validate_and_reserialize_sql(case.ground_truth_sql)
            except GuardrailViolation as exc:
                results.append(EvalResult(case, False, f"ground truth SQL rejected by guardrail: {exc}", None))
                continue

            async with pool.acquire() as conn:
                truth_rows = await conn.fetch(truth_sql)

            try:
                query_result = await orchestrator.answer(case.question, catalog, [], max_retries=2)
            except OrchestratorError as exc:
                results.append(EvalResult(case, False, f"orchestrator failed: {exc}", None))
                continue

            if query_result.response_type != "query":
                # Every golden-set question is complete analytical phrasing and should
                # never be classified as conversational/needs-clarification - if this
                # fires, it's a real classifier regression, not an expected outcome.
                results.append(
                    EvalResult(
                        case, False,
                        f"expected a query, got response_type={query_result.response_type!r} "
                        f"({query_result.narrative_text!r})",
                        None,
                    )
                )
                continue

            async with pool.acquire() as conn:
                generated_rows = await conn.fetch(query_result.sql_executed)

            match = _result_sets_match(truth_rows, generated_rows)
            reason = "match" if match else f"result mismatch ({len(generated_rows)} vs {len(truth_rows)} rows)"
            results.append(EvalResult(case, match, reason, query_result.sql_executed))

        return results
    finally:
        await pool.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", default="mock", choices=["mock", "gemini"])
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.9,
        help="Minimum required Execution Accuracy (0-1). Default 0.9 matches the "
        "project spec's stated CI gate - see eval/README.md.",
    )
    args = parser.parse_args()

    results = asyncio.run(run_eval(args.provider))

    passed = sum(1 for r in results if r.passed)
    total = len(results)
    accuracy = passed / total if total else 0.0

    print(f"\nExecution Accuracy: {passed}/{total} ({accuracy:.1%})\n")
    for r in results:
        status = "PASS" if r.passed else "FAIL"
        print(f"  [{status}] {r.case.id}: {r.case.question}")
        if not r.passed:
            print(f"         reason: {r.reason}")
            if r.generated_sql:
                print(f"         generated: {r.generated_sql}")
            print(f"         ground truth: {r.case.ground_truth_sql}")

    if accuracy < args.threshold:
        print(f"\nFAILED: accuracy {accuracy:.1%} is below threshold {args.threshold:.1%}")
        return 1

    print(f"\nOK: accuracy {accuracy:.1%} meets threshold {args.threshold:.1%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
