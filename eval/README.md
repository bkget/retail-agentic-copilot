# Evaluation harness

`eval_harness.py` computes **Execution Accuracy (EX)**: for each question in
`golden_set.jsonl`, it runs the question through the real orchestrator (LLM generation +
AST guardrail + execution, exactly as the API does) and compares the result set against
an independently-authored ground-truth query, executed through the same guardrail. It is
not a mock of the pipeline - both queries hit the real Postgres semantic layer.

## Running it

```bash
# from repo root, with retail_copilot_db running (docker compose up -d retail_copilot_db)
export POSTGRES_HOST=localhost POSTGRES_PORT=5433 \
       POSTGRES_AGENT_PASSWORD_FILE=./secrets/agent_password.txt
python eval/eval_harness.py --provider mock       # 30/30 (100%) as of this writing
python eval/eval_harness.py --provider gemini      # requires GEMINI_API_KEY(_FILE)
```

Exits non-zero if accuracy falls below `--threshold` (default 0.9, matching the
project spec's stated CI gate) - see `.github/workflows/eval.yml` for how this gates PRs.

## Scope, honestly stated

The golden set currently has **30 questions**, not the spec's stated 100. The
methodology, harness, and CI gate are complete and real; growing the count from here is
mechanical (author more `{question, ground_truth_sql}` pairs following the existing
pattern) rather than a design gap. Coverage across question shapes is deliberate:
revenue/units/transaction-count/average metrics, all filterable dimensions on both
semantic-layer tiers, year filters, and "top N" queries.

## What building this actually found

Authoring the golden set - specifically, having to write ground-truth SQL independently
rather than just trusting whatever the mock produced - surfaced three real bugs that unit
tests hadn't caught:

1. **Non-deterministic `SUM()`.** Running the identical query twice back-to-back returned
   different totals (e.g. `40764592.0` vs `40764620.0` for the same division/year). Root
   cause: `total_price`/`unit_price` were `real` (float4) in the source data, and Postgres's
   parallel aggregation combines per-worker partial sums in a run-dependent order - float
   addition isn't associative, so the accumulated rounding error varied between runs. Fixed
   by casting to `numeric` in the semantic layer (`db/init/04_semantic_views.sql`); numeric
   addition is exact, so the result no longer depends on summation order. This wasn't a
   nice-to-have - it directly contradicted the project's "no math hallucinations" premise,
   since the same question could show two different revenue totals to the same user.
2. **"country" contains "count".** A naive `"count" in question.lower()` substring check
   routed "Revenue by manufacturer **country**" to `COUNT(*)` instead of `SUM(revenue)`,
   because "country" literally contains "count" as a substring. Same root cause as an
   earlier bug caught during manual testing ("average **order** value" routing to a
   transaction count). Fixed by switching every keyword check in `MockLLMProvider` to
   whole-word regex matching (`app/agent/llm_provider.py::_word_in`).
3. **"payment" beat "bank" on ambiguity.** "Revenue by payment **bank**" matched the more
   generic `payment` keyword before ever checking `bank`, because dict iteration order was
   the tie-breaker and "payment" came first. Fixed by explicitly ordering keyword checks
   most-specific-first.

All three are covered by regression tests in `backend/tests/`, not just fixed and left
undocumented.

## Ground truth vs. generated SQL: why they're allowed to differ

Execution Accuracy compares **result sets**, not SQL text. Ground truth is written by hand
against the semantic layer as an independent check on correctness - it does not need to
(and often doesn't) match the generated SQL's exact table/expression choice, only its
output. Where the two must agree by construction (e.g. "average" queries must run against
`mv_sales_analysis`, never the pre-aggregated rollup, or the numbers are answering a
different question) - that agreement is itself the thing being tested.
