# Retail Agentic Copilot

![CI](https://github.com/bkget/retail-agentic-copilot/actions/workflows/ci.yml/badge.svg)
![Python](https://img.shields.io/badge/python-3.11-3776AB?logo=python&logoColor=white)
![Next.js](https://img.shields.io/badge/next.js-14-black?logo=next.js&logoColor=white)
![PostgreSQL](https://img.shields.io/badge/postgresql-17-4169E1?logo=postgresql&logoColor=white)
![Docker](https://img.shields.io/badge/docker-compose-2496ED?logo=docker&logoColor=white)

**Conversational analytics over a real 1,000,000-row PostgreSQL retail sales dataset.**
Ask a question in plain English; a Google ADK / Gemini Flash agent turns it into SQL
against a governed semantic layer, an independent AST guardrail validates it before it
ever executes, and the answer streams back live with a narrative whose numbers are
computed in Python - never guessed by the LLM - alongside a typed chart.

> Built from a written spec, then deliberately run against **real** data instead of
> synthetic fixtures. That choice surfaced correctness and security issues the spec's
> own example code didn't anticipate - see [What Real Data Caught](#what-real-data-caught).

## Table of Contents

- [Features](#features)
- [Tech Stack](#tech-stack)
- [Architecture](#architecture)
- [Quickstart](#quickstart)
- [Project Structure](#project-structure)
- [Security Model](#security-model)
- [Testing](#testing)
- [What Real Data Caught](#what-real-data-caught)
- [Known Limitations](#known-limitations)

## Features

- **Actually conversational** - greetings and "what can I ask?" are answered
  directly, vague questions get a clarifying question instead of a guessed answer, and
  follow-ups ("only for 2024", "explain that again") reuse the prior turn's context.
- **AST-validated SQL guardrail** - every generated query is parsed with `sqlglot`,
  checked against a table/function allow-list, and re-serialized before execution.
  Blocks CTE-wrapped DML, forces a row `LIMIT`, and never executes the model's raw text.
- **Deterministic, hallucination-safe narrative** - every number in the response is
  read directly from the SQL result set. The LLM's only job is generating the query;
  it never does the arithmetic.
- **PII-safe, two-tier semantic layer** - a pre-aggregated rollup view for common
  questions and a row-level view for drill-downs, both built over a real 1M-row dataset
  with customer data excluded entirely.
- **Live streaming, not spinners** - Server-Sent Events push status → generated SQL →
  narrative tokens → chart → timing metadata to the UI as each stage completes.
- **Full audit trail** - every answer ships with the exact SQL executed, a
  guardrail/LLM/DB timing breakdown, and an OpenTelemetry trace ID.
- **Self-correcting generation** - a guardrail rejection or DB error is fed back to
  the LLM for a bounded retry instead of failing the request outright.
- **Evaluated, not vibes-tested** - an Execution-Accuracy harness against a growing
  golden question set gates CI at ≥90% before merge.
- **Swappable LLM backend** - ships with a deterministic rule-based router (no API
  key needed to run the whole stack) or real Gemini via Google ADK, behind one interface.

## Tech Stack

| Layer | Technology |
|---|---|
| Backend | FastAPI (Python 3.11, async), asyncpg |
| Agent / LLM | Google ADK + Gemini Flash (swappable via `LLMProvider`) |
| SQL safety | `sqlglot` AST parsing/validation and re-serialization |
| Database | PostgreSQL 17, materialized-view semantic layer, APScheduler-driven refresh |
| Frontend | Next.js 14, React, Recharts, Server-Sent Events client |
| Observability | OpenTelemetry tracing |
| Infra | Docker Compose (Postgres + backend + frontend) |
| CI | GitHub Actions - backend tests, eval-accuracy gate, frontend build |

## Architecture

<img src="./architecture.png" alt="Retail Agentic Copilot Architecture" width="100%">

**Request path:** browser → FastAPI `/api/query` (SSE) → intent classification →
LLM SQL generation → AST guardrail → PostgreSQL (`agent_ro`, read-only) → deterministic
narrative + chart config → streamed back to the browser.

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

Then open **http://localhost:3000**. Try:

- *"total revenue by division in 2020"*
- *"top 5 items by revenue"*
- *"average order value"*
- *"which years do you have data for?"*

No Gemini API key is required - `LLM_PROVIDER=mock` (the default) uses a real
rule-based NL→SQL router (`backend/app/agent/llm_provider.py::MockLLMProvider`), not a
canned demo. It scores **100% Execution Accuracy** on the golden question set. To use
real Gemini instead, populate `secrets/gemini_api_key.txt` and set `LLM_PROVIDER=gemini`
on the `agent_backend` service in `docker-compose.yml`.

## Project Structure

```
db/          DDL, seed data, and the semantic layer      → db/README.md
backend/     FastAPI + guardrail + orchestrator + agent   → backend/tests/
frontend/    Next.js 14 chat UI, SSE client, Recharts
eval/        Execution-Accuracy harness                   → eval/README.md
.github/     CI: backend tests + eval gate + frontend build, on every PR
```

## Security Model

- **`agent_ro`** connects with `NOSUPERUSER NOCREATEDB NOINHERIT`,
  `default_transaction_read_only = on`, an 8s `statement_timeout`, and `SELECT`-only
  access to exactly two views - no grants on the normalized `core.*` schema at all.
- **Generated SQL is never executed as-is.** It's parsed, checked against a table
  allow-list (schema *and* name - a same-named table in the wrong schema is still
  rejected), a function allow-list, and a full-tree walk that blocks
  `INSERT/UPDATE/DELETE/DROP/CREATE/ALTER/SET/MERGE/COPY/GRANT` even nested inside a CTE.
  Only the **re-serialized AST** is executed, never the model's original text - closing
  parser-differential attacks between `sqlglot`'s dialect and Postgres's own parser.
- **Secrets are files** (Docker secrets pattern), never literals in code or compose YAML.
- **Per-session rate limiting** on the query endpoint - cheap insurance against runaway
  LLM spend from the retry loop.

## Testing

```bash
docker compose up -d postgres_db          # needed for all of the below - real DB, no mocks
cd backend && pip install -e ".[dev]"
pytest                                     # full suite, real DB, ~25s
pytest -m slow                             # + the real MV refresh integration test, ~90s
cd .. && python eval/eval_harness.py --provider mock   # Execution Accuracy report
```

## What Real Data Caught

The original spec was a reasonable starting architecture, but several of its concrete
details didn't survive contact with a real 1M-row dataset or a real security review.
Every item below is fixed here, with a regression test:

1. **Function allow-list didn't allow-list anything real.** `sqlglot` represents known
   SQL functions (`SUM`, `CAST`, `EXTRACT`, ...) as dedicated `exp.*` node classes, not
   `exp.Anonymous`. A check that only inspects `exp.Anonymous.name` - the spec's
   approach - never actually enforces the allow-list against any function `sqlglot`
   recognizes, only against ones it doesn't. Fixed in `ast_guardrail.py::_function_name`.
2. **PII was reachable through two separate paths.** The source database's read-only
   role had direct `SELECT` on the customer table (name, contact, national ID), and the
   semantic-layer view itself exposed raw customer name/contact - both bypassing the
   "PII-safe semantic layer" the spec described as its whole point. Fixed by excluding
   customer data from the semantic layer entirely and building the read-only role fresh.
3. **Non-deterministic `SUM()`.** The source data's `real` (float4) money columns made
   `SUM(total_price)` return a different total on every run - parallel-worker partial
   sums combine in a non-fixed order, and float addition isn't associative. This
   directly contradicted the "no math hallucinations" goal. Fixed by casting to
   `numeric` in the semantic layer - only caught by building the eval harness, not by
   unit tests.
4. **Hardcoded enum hints didn't match the real data.** The spec's system-prompt example
   assumed Title-Case divisions and a `mobile_banking` payment type. The real data has
   7 UPPERCASE divisions and `payment_type ∈ {card, cash, mobile}`. Fixed by loading
   enum hints from `SELECT DISTINCT` against the live views at startup - hand-typed
   hints can't drift from reality if they're never hand-typed.
5. **Whole-word vs. substring keyword matching.** "average **order** value" matched a
   `count` keyword because `order` is a substring hit; "manufacturer **country**"
   matched `count` for the same reason. Fixed by switching every keyword check to
   whole-word regex matching.
6. **No self-correction loop.** One-shot generation had no retry path if the guardrail
   rejected the query or execution failed. The orchestrator now retries with the
   specific error fed back to the LLM.
7. **No real materialized-view refresh mechanism**, despite the response contract
   having a `last_refreshed_at` field. Fixed with a scheduled job on a dedicated
   refresh-only role - deliberately not the same role the query path uses, so a
   compromised query path can never trigger or interfere with a refresh.
8. **No session state, despite "conversational" being the product name.** Added a
   minimal in-memory session store - deliberately not Redis, correct for a
   single-instance deployment, and the first thing to swap out if this ran with more
   than one backend replica.

## Known Limitations

- **`GeminiADKProvider` is unverified against a live API call.** No Gemini key was
  available while building this. The `google-adk` call shape was checked against the
  actually-installed package's real signatures, not written from memory - but
  end-to-end behavior with a real key is genuinely untested. The `LLMProvider`
  interface exists specifically so this is a contained, swappable risk: everything else
  is proven independent of it via the mock provider.
- **Eval golden set is 30 questions, not 100.** The harness, methodology, and CI gate
  are complete; growing the count is mechanical authoring, not a design gap.
- **Frontend depends on Next.js 14.x**, which has several disclosed CVEs not fully
  patched until the (breaking) Next 16 line. Acceptable for a project that runs locally
  and isn't exposed to the internet; would need addressing before any real deployment.
- **Session store is process-local**, not shared across replicas - fine for one backend
  instance, explicitly wrong for horizontal scaling without first swapping it for Redis.
