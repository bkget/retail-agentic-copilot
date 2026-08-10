# Enterprise Agentic Data Copilot

Conversational analytics over a real 1,000,000-row PostgreSQL sales dataset. Natural
language goes in; a Google ADK / Gemini Flash agent generates SQL against a governed
semantic layer, an AST-based guardrail independently validates it before it ever
executes, and the answer streams back over SSE with a narrative whose numbers are
computed in Python — never by the LLM — alongside a typed chart config.

This project started from a written spec (kept in git history / design notes) and was
deliberately built against a **real** dataset rather than synthetic fixtures. That choice
surfaced several correctness and security issues the spec's own example SQL/DDL didn't
anticipate — they're fixed here, and documented below and in `db/README.md` /
`eval/README.md`, because *finding* them by actually running the system against real
data (not just reading the spec) is the interesting part of this project.

## Quickstart

```bash
cp secrets/postgres_superuser_password.txt.example secrets/postgres_superuser_password.txt
cp secrets/agent_password.txt.example secrets/agent_password.txt
cp secrets/refresher_password.txt.example secrets/refresher_password.txt
# edit those three files to real random values if you're doing more than a local demo

docker compose up -d
# first start takes ~10-15 min: postgres_db loads 1M seed rows and builds the semantic
# layer views before it reports healthy - watch `docker compose logs -f postgres_db`
```

Then open **http://localhost:3000**. Try: *"total revenue by division in 2020"*,
*"top 5 items by revenue"*, *"average order value"*.

No Gemini API key is required to run this — `LLM_PROVIDER=mock` (the default) uses a
real rule-based NL→SQL router (`backend/app/agent/llm_provider.py::MockLLMProvider`),
not a canned demo. It scores **100% Execution Accuracy** on `eval/golden_set.jsonl`. To
use real Gemini instead: populate `secrets/gemini_api_key.txt` and set
`LLM_PROVIDER=gemini` on the `agent_backend` service in `docker-compose.yml`.

## Architecture

```
 ┌─────────────────────────────┐
 │   NEXT.JS 14 FRONTEND        │   fetch+ReadableStream SSE client (EventSource can't
 │   Recharts, SQL/latency      │   POST a body) · Recharts bar/line via validated
 │   audit drawer               │   categorical palette (see dataviz skill)
 └──────────────┬───────────────┘
                │ POST /api/query → SSE: status, sql, narrative_delta, visualization,
                │                        metadata, done
 ┌──────────────▼───────────────┐
 │   FASTAPI BACKEND             │
 │   ┌─────────────────────┐    │   session store (in-memory, TTL) · token-bucket
 │   │ Orchestrator          │  │   rate limiter · OTel spans per phase, real trace_id
 │   │  generate → guardrail │  │   threaded into the SSE metadata event
 │   │  → execute → retry    │  │
 │   │  (max 2, error fed    │  │
 │   │  back to the LLM)     │  │
 │   └──────────┬────────────┘  │
 │   LLMProvider (seam):        │   MockLLMProvider (real rule-based router) or
 │   generate_sql() only         │  GeminiADKProvider (google-adk LlmAgent+Runner) -
 │                                │  orchestrator/guardrail/tests never change either way
 └──────────────┬───────────────┘
                │ re-serialized AST, never the model's raw text
 ┌──────────────▼───────────────┐
 │   AST GUARDRAIL (sqlglot)     │   shape check (top-level must be SELECT) + full-tree
 │   security/ast_guardrail.py   │   walk (catches CTE-wrapped DML) + table allow-list
 │                                │  (schema, name) + function allow-list (covers typed
 │                                │  nodes like SUM/CAST, not just unrecognized ones -
 │                                │  see "what the spec got wrong" below) + LIMIT 500 cap
 └──────────────┬───────────────┘
                │ agent_ro (NOSUPERUSER, read-only txn, 8s statement_timeout)
 ┌──────────────▼───────────────┐
 │   POSTGRESQL (1M rows)        │
 │   core.*        - normalized, unreachable by agent_ro (incl. all PII)
 │   public.mv_sales_analysis    - Tier 2, row-level, PII-free, numeric money columns
 │   public.mv_sales_daily_rollup- Tier 1, pre-aggregated, default for most questions
 │   refresh_semantic_views()     - owned by refresher_rw, on an APScheduler timer
 └───────────────────────────────┘
```

## What the spec got wrong, and what running it against real data caught

The original written spec was a reasonable starting architecture, but several of its
concrete details didn't survive contact with a real 1M-row dataset or a real security
review. Fixed here, each with a regression test:

1. **Function allow-list didn't allow-list anything real.** `sqlglot` represents known
   SQL functions (`SUM`, `CAST`, `EXTRACT`, ...) as dedicated `exp.*` node classes, not
   `exp.Anonymous`. A check that only inspects `exp.Anonymous.name` — the spec's
   approach — never actually enforces the allow-list against any function sqlglot
   recognizes, only against ones it doesn't. Fixed in `ast_guardrail.py::_function_name`,
   covered by `tests/test_ast_guardrail.py::TestFunctionAllowList`.
2. **PII was reachable through two separate paths.** The live source database's
   `agent_readonly` role had direct `SELECT` on `core.customer_dim` (name, contact,
   national ID), and the semantic-layer view itself exposed raw customer name/contact —
   both bypassing the "PII-safe semantic layer" the spec described as its whole point.
   Fixed by excluding customer data from the semantic layer entirely (no analytical need
   for it here) and building `agent_ro` fresh with grants only on the two public views.
3. **Non-deterministic `SUM()`.** The source data's `real` (float4) money columns made
   `SUM(total_price)` return a different total on every run — parallel-worker partial
   sums combine in a non-fixed order, and float addition isn't associative. This directly
   contradicted the "no math hallucinations" goal. Fixed by casting to `numeric` in the
   semantic layer. Full story in `db/README.md` and `eval/README.md` — this one was
   only caught by building the eval harness, not by unit tests.
4. **Hardcoded enum hints didn't match the real data.** The spec's system-prompt example
   assumed Title-Case divisions including "Mymensingh" and `payment_type` of
   `mobile_banking`. The real data has 7 UPPERCASE divisions (no Mymensingh) and
   `payment_type` ∈ `{card, cash, mobile}`. Fixed by loading enum hints from
   `SELECT DISTINCT` against the live views at startup (`app/schema/catalog.py`),
   refreshed alongside the materialized views — hand-typed hints can't drift from
   reality if they're never hand-typed.
5. **Whole-word vs. substring keyword matching.** Two separate bugs from the same root
   cause: "average **order** value" matched a `count` keyword because `order` is a
   substring hit, and "manufacturer **country**" matched `count` for the same reason.
   Fixed by switching every keyword check to whole-word regex matching
   (`llm_provider.py::_word_in`). See `eval/README.md` for the full list of bugs the
   eval harness caught this way.
6. **No self-correction loop.** One-shot generation with no retry path if the guardrail
   rejects the query or execution fails. `Orchestrator.answer` now retries up to twice,
   feeding the specific error back to the LLM.
7. **No real MV refresh mechanism**, despite the response contract having a
   `last_refreshed_at` field. `pg_cron` wasn't even installed on the source database.
   Fixed with an APScheduler job on a separate `refresher_rw` connection — deliberately
   not the same role the query path uses, so a compromised query path can never trigger
   or interfere with a refresh.
8. **No session state, despite "conversational" being the product name.** Added a
   minimal in-memory session store. Deliberately *not* Redis — correct for a
   single-instance deployment, and explicitly the first thing to swap out if this ran
   with more than one backend replica.

## Security model

- `agent_ro`: `NOSUPERUSER NOCREATEDB NOINHERIT`, `default_transaction_read_only = on`,
  `statement_timeout = 8s`, `idle_in_transaction_session_timeout = 15s`. `SELECT`-only on
  exactly two views. No grants on `core.*` at all — verified in CI
  (`tests/test_ast_guardrail.py` + a live `permission denied` check during development).
- Generated SQL is never executed as-is: it's parsed, validated against a table
  allow-list (schema **and** name, so `core.mv_sales_analysis` — same name, wrong
  schema — is still rejected), a function allow-list, and a full-tree walk that blocks
  `INSERT/UPDATE/DELETE/DROP/CREATE/ALTER/SET/MERGE/COPY/GRANT` even when nested inside a
  CTE of an otherwise-`SELECT`-shaped statement. Only the **re-serialized AST** is
  executed, never the model's original text — closes parser-differential attacks between
  sqlglot's dialect and Postgres's own parser.
- Secrets are files (Docker secrets pattern), never literals in code or compose YAML.
- Per-session token-bucket rate limiting on the query endpoint (cheap insurance against
  runaway LLM spend from the retry loop).

## Repository layout

```
db/          DDL, seed data, and the semantic layer - see db/README.md
backend/     FastAPI + guardrail + orchestrator + agent providers - see backend/tests/
frontend/    Next.js 14 chat UI, SSE client, Recharts
eval/        Execution Accuracy harness against the real semantic layer - see eval/README.md
.github/     CI: backend test suite + eval harness gate + frontend build, on every PR
```

## Testing

```bash
docker compose up -d postgres_db          # needed for all of the below - real DB, no mocks
cd backend && pip install -e ".[dev]"
pytest                                     # 60 tests, ~25s
pytest -m slow                             # + the real MV refresh integration test, ~90s
cd .. && python eval/eval_harness.py --provider mock   # 30/30 Execution Accuracy
```

## Known limitations, stated plainly

- **`GeminiADKProvider` is unverified against a live API call.** No Gemini key was
  available while building this. The `google-adk` call shape (`LlmAgent` / `Runner` /
  `InMemorySessionService` / `run_async` / `Event.is_final_response()`) was checked
  against the actually-installed package's real signatures, not written from memory —
  but end-to-end behavior with a real key is genuinely untested. The `LLMProvider`
  interface exists specifically so this is a contained, swappable risk: everything else
  (guardrail, orchestrator, retry loop, narrative, tests) is proven independent of it via
  `MockLLMProvider`.
- **Eval golden set is 30 questions, not 100.** The harness, methodology, and CI gate are
  complete; growing the count is mechanical authoring, not a design gap. See
  `eval/README.md`.
- **Frontend depends on Next.js 14.x**, which has several disclosed CVEs not fully
  patched until the (breaking) Next 16 line. Acceptable for a project that runs locally
  and isn't exposed to the internet; would need addressing before any real deployment.
- **Session store is process-local**, not shared across replicas — fine for one backend
  instance, explicitly wrong for horizontal scaling without first swapping it for Redis.
