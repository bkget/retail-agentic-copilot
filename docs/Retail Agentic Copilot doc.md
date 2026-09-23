# Retail Agentic Copilot — Complete Documentation

## What This Project Is

The Retail Agentic Copilot is a conversational analytics assistant over a 1-million-row PostgreSQL retail sales dataset. You type a question in plain English ("total revenue by division in 2020", "compare 2019 and 2020 by district") and it generates SQL, validates that the SQL is safe, runs it read-only, and streams back a plain-language answer with a chart — showing its work (a reasoning trace, the exact SQL, timing) along the way.

Three design commitments run through every part of the system:

1. **No paid LLM key required.** The default reasoning engine (`MockLLMProvider`) is a deterministic, rule-based NL→SQL mapper — synonym tables and regex, no embeddings, no network calls. It costs nothing to run and always produces the same SQL for the same question, which is also what makes automated evaluation possible (see Testing and Evaluation below). A real LLM (Gemini, via Google's Agent Development Kit) is wired in as an optional alternative, and an even lighter-weight *optional* LLM fallback can rewrite unusual phrasings into the mock provider's vocabulary — never used to write SQL or compute numbers.
2. **SQL is never trusted as text.** Whatever produces the SQL string (mock or real LLM), it is re-parsed into an AST, validated against a strict table/function allow-list, and re-serialized before it is ever sent to Postgres. See "SQL Generation and the AST Guardrail" — this is the load-bearing security mechanism of the whole project.
3. **No math hallucinations.** Every number in the final answer is read directly out of the SQL result set by deterministic Python code — never estimated or paraphrased by a model. If the guardrailed query didn't return a number, the narrative can't state it.

The project is a full stack: PostgreSQL with a two-tier semantic layer, a FastAPI backend that streams answers over Server-Sent Events, and a Next.js frontend with a ChatGPT-style "thinking" trace and Recharts visualizations.

## Architecture at a Glance

Three services, run with Docker Compose:

- **`retail_copilot_db`** — PostgreSQL 17. Holds a normalized 3NF `core` schema (the raw dataset) and a `public` schema exposing only a two-tier semantic layer as materialized views.
- **`retail_copilot_backend`** — FastAPI (Python, async). Owns the whole agent pipeline and exposes one streaming endpoint, `POST /api/query`.
- **`retail_copilot_frontend`** — Next.js 14 (App Router). A single-page chat UI that proxies `/api/*` to the backend same-origin, so the browser never talks cross-origin.

An optional fourth service, `retail_copilot_redis` (profile `redis`), lets conversation state survive across multiple backend replicas instead of living in backend process memory.

**The request lifecycle, end to end**, for one question:

1. The browser POSTs `{question, session_id}` to `/api/query`. The response is a `text/event-stream` — the connection stays open and the backend pushes frames as work completes (`backend/app/main.py`).
2. The orchestrator (`backend/app/agent/orchestrator.py`) loads the session's prior state, then **classifies** the message (greeting? schema question? a genuine analytical question? an unanswerable concept? a bare fragment like "revenue"?).
3. If it's analytical, the message is **mapped** to schema terms (a metric, an optional dimension, filters, a year) and **merged** with whatever the conversation already established (`resolve_with_session`).
4. The resolved query is turned into **SQL** by the active provider (`MockLLMProvider` by default), which is then re-parsed and validated by the **AST guardrail** — only then does it touch the database.
5. The validated SQL runs against the appropriate semantic-layer view. The result rows are turned into a **narrative** (plain English, every number real) and a **visualization** config (bar/line/multi-line/grouped-bar/table), both computed deterministically from the rows.
6. All of this streams back as a sequence of SSE frames — status updates, live reasoning "steps", the SQL, the narrative typed out chunk by chunk, the chart config, follow-up suggestions, and a final consolidated `done` frame.
7. The frontend (`ChatStream.tsx`) consumes the stream and renders it live: a collapsible "Thinking..." trace, the typed-out answer, the chart, and a `SQL & trace` audit drawer.

Every later section of this guide expands one stage of that lifecycle.

## The Data Layer

**`db/init/01_schema_core.sql`** defines the normalized source schema in the `core` schema: `customer_dim`, `item_dim`, `store_dim`, `payment_dim`, `time_dim`, and a `fact_table` with foreign keys into each dimension. This schema mirrors the real dataset (1,000,000 fact rows, gzipped in `db/seed/`) and is **never exposed to the agent** — it's the raw source, not the query surface.

**`db/init/04_semantic_views.sql`** builds the two-tier semantic layer in `public`, which is the *only* thing the agent can see:

- **`mv_sales_analysis`** (Tier 2) — a row-level "one big table" joining fact + all four non-customer dimensions. Used for anything the pre-aggregated rollup can't answer: averages, payment bank/type, manufacturer country, quarter-level breakdowns.
- **`mv_sales_daily_rollup`** (Tier 1) — pre-aggregated by date × division × district × item, with `total_revenue`, `total_units_sold`, `transaction_count`. This is the default for most questions — much less data to scan.

Both are materialized views (`REFRESH MATERIALIZED VIEW CONCURRENTLY`, so queries keep running during a refresh), refreshed on a timer by `backend/app/refresh/scheduler.py` and tracked in a `refresh_log` table that backs the "data last refreshed at" timestamp in the API.

Three corrections baked into the semantic layer, each the result of a real bug found while building this:

- **`total_price`/`unit_price` are cast to `numeric`, not left as the source `real` (float4).** Running the identical `SUM(total_price)` query twice back to back returned *different* totals (e.g. `40764592.0` vs `40764620.0`), because Postgres's parallel aggregation combines per-worker partial sums in a non-fixed order and float4 addition isn't associative. This directly contradicted the "no math hallucinations" goal — `numeric` addition is exact, so the fix makes `SUM()` deterministic regardless of the parallel plan chosen.
- **`fact_key` is the fact table's own `bigserial` surrogate key, not a hash** of the dimension keys. The original design hashed `item|store|payment|time` keys for a PII-safe row id, but that scheme collided twice in the real 1M-row dataset and would have embedded `customer_key` if it were made unique by including it.
- **`sale_date` is a real `date`**, parsed from the source's text `'DD-MM-YYYY HH24:MI'` format via `to_date()`.

Customer data (`core.customer_dim`, including name/contact/NID) is **excluded from the semantic layer entirely** — not filtered at query time, simply never joined in. There is no code path by which the agent could ever see it.

## Database Security

`db/init/03_roles.sh` creates two non-superuser Postgres roles, each with the minimum privileges its job needs:

- **`agent_ro`** — what the backend's query path connects as. `NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT`, `GRANT SELECT` on only the two semantic-layer views (never on `core.*`), and `REVOKE ALL ON SCHEMA core` stated explicitly as defense in depth. It also gets **database-level runtime limits** independent of anything the application code does: `default_transaction_read_only = on`, `statement_timeout = 8s`, `idle_in_transaction_session_timeout = 15s`. Even if every layer of application-level validation were somehow bypassed, this role physically cannot write, and a runaway query is killed by Postgres itself.
- **`refresher_rw`** — a separate role that owns the materialized views and refreshes them on a schedule. Kept distinct from `agent_ro` specifically so a compromised query path can never trigger or interfere with a refresh, and a bug in the refresh job can never touch the query path's permissions.

Passwords are injected via Docker secrets (`POSTGRES_AGENT_PASSWORD_FILE`, etc.) or plain env vars as a local-dev fallback — never hardcoded, and `app/config.py`'s `_resolve_secret()` prefers the `_FILE` form when both are set.

This is layered security, not a single check: even if the AST guardrail (next section) had a bug, `agent_ro`'s own database-level permissions and session limits are a second, independent backstop.

## The Agent Pipeline Overview

Everything lives under `backend/app/agent/`. The pipeline is a formal, tool-shaped sequence — the same shape whether the reasoning engine is the deterministic mock or a real LLM (`LLMProvider` is an abstract base both implement):

1. **`classify_input(question)`** — decides what kind of message this is: `GREETING`, `SCHEMA_INFO`, `DATABASE_QUERY`, `CLARIFICATION` (a bare fragment like "revenue"), or `UNKNOWN`. Deliberately stateless — it never sees session state, only the message text.
2. **`map_terms_to_columns(question, catalog)`** — resolves the question's words into real schema columns: a metric, an optional dimension (and second dimension), named-value filters, years, top-N. Returns `None` for anything not mentioned, so the caller can distinguish "not mentioned" (inheritable from prior context) from "mentioned but unmappable."
3. **`resolve_with_session(mapping, signals, state)`** (`llm_provider.py`, called from the orchestrator, not part of the provider interface) — merges this turn's explicit mapping with `SessionState` left over from prior turns. Whatever the message explicitly says always wins; a silent slot is inherited only when the message actually references prior context.
4. **`generate_sql(question, catalog, resolved, error_feedback)`** — builds the SQL string from the resolved query. `error_feedback` carries a prior attempt's guardrail/DB error back in, for self-correction retries.
5. **AST guardrail** (`app/security/ast_guardrail.py`) — re-parses and validates the SQL before it ever reaches Postgres.
6. **Execute** — runs the validated SQL against `agent_ro`'s connection pool.
7. **`build_narrative`** / **`build_visualization`** (`narrative.py`, `visualization.py`) — turn the result rows into plain English and a chart config, both purely from the rows.

`Orchestrator` (`orchestrator.py`) is the class that drives all of this per turn (`answer()` → `_route()`), holds a per-session `asyncio.Lock` so two overlapping requests for the same session can't interleave state updates, and emits a `{id, label, detail, status, elapsed_ms}` event at the start and end of every stage via `on_step` — this is exactly what powers the frontend's live "Thinking..." trace. Up to `max_retries` (default 2) SQL-generation attempts are made per turn before giving up with a friendly error.

## Intent Classification and Term Mapping

`MockLLMProvider` (`backend/app/agent/llm_provider.py`) is a deterministic, rule-based implementation of the tool pipeline — no embeddings, no ML. "Semantic mapping" here means a comprehensive, hand-maintained synonym table, not open-ended NLU; that's a disclosed, intentional scope boundary, not a stand-in for a real model.

**Synonym tables** map paraphrases to real columns and metric aliases — e.g. `store_division` matches "division", "department", "business unit", "segment", "region"; `total_revenue` matches "revenue", "income", "turnover", "earnings". Order matters: the dimension list checks `payment_bank` before the more generic `payment_type`, so "revenue by payment bank" resolves to the specific column, not the generic one.

**Whole-word matching (`_word_in`), never substring, is the single most important correctness detail in this file.** The evaluation harness caught two real bugs from naive substring checks: "manufacturer **country**" contains "count" and was routing to `COUNT(*)` instead of `SUM(revenue)`; "average **order** value" contains "order" and was routing to a transaction count instead of `AVG()`. Every keyword lookup in this module goes through `_word_in`, which matches on a word boundary (allowing a trailing "s"), specifically to prevent this class of bug from recurring.

**Other things this module detects, beyond the basic metric/dimension mapping:**

- **Superlatives over a singular entity** (`wants_single_top`) — "the maximum revenue generating store" implies exactly one result (`top_n = 1`), distinct from "top stores" (plural), which implies a ranked list.
- **Explicit chart preference** (`detect_chart_preference`) — "...using a line chart", "...as a table" is parsed out and honored later by `apply_chart_preference` in `visualization.py`, when the data shape actually supports it.
- **Named-value filters** (`_extract_named_filters`) — quoted phrases (`'Dhaka'`) or bare enum values ("cash transactions") are matched against the *real* distinct values loaded from the database at startup (`SchemaCatalog.enum_hints`), never a hardcoded list — so a filter can't silently drift from what the data actually contains. Two or more matched values on the same column ("compare 'Dhaka' and 'Chittagong'") force that column to become the GROUP BY target instead of being excluded as a filter.
- **Tier selection** — `avg_revenue` and any Tier-2-only dimension (supplier, manufacturer country, payment bank, quarter) force the query onto `mv_sales_analysis`; everything else prefers the cheaper `mv_sales_daily_rollup`.

`GeminiADKProvider` (`gemini_provider.py`) implements the same three-method interface with a real Gemini model (via Google's Agent Development Kit) doing the classification/mapping/SQL-writing judgment calls — but deliberately reuses the mock provider's *deterministic* helpers for named-value filter matching, since re-asking a model to verify "does this question quote a real division name" only adds a hallucination surface with no upside.

## Conversation Memory and Slot-Filling

This section documents the fix for the original bug that kicked off this whole engagement: the assistant would ask "which time period?", the user would reply "2020", and it would ask the identical question again — forever. The root cause was that a bare reply like "2020" has no metric and no dimension of its own, so it looked exactly like a fresh, unanswerable fragment rather than an answer to the question just asked.

**`SessionState`** (`llm_provider.py`) is what the agent currently believes the conversation is "about": `last_metric_alias`, `last_dimension`, `last_extra_dimension`, `last_year`, `active_filters`, and — the key addition — **`pending: PendingClarification | None`**.

**`PendingClarification`** is set whenever the assistant's last reply asked a clarifying question. It records the `kind` (`"breakdown"` — asked how to slice a metric; `"metric"` — asked which measure; `"confirm_rewrite"` — offered a reshaped question and asked yes/no), the original question, and the partially-resolved query built so far.

**The fix, concretely** (`Orchestrator._resolve_pending`): when a session has a pending clarification, the *next* message is first interpreted as an **answer to it**, not as a brand-new question. "2020" is parsed through `map_terms_to_columns` like anything else, but instead of asking "does this alone form a complete question?", the orchestrator merges whatever it contributes (a year, in this case) into the `partial` query saved on the pending clarification, then proceeds straight to running that combined query. A clarification is never asked twice for the same gap: if the reply still leaves something unresolved (no metric was ever named), a sensible default is applied and the assumption is stated in the reply ("You didn't name a measure, so I used total revenue.").

**`resolve_with_session`** (used for ordinary follow-ups, not clarification answers) applies a stricter rule: an explicitly complete question (both a metric and a dimension named) is always treated as fresh and standalone, even if it also happens to contain a reference word like "previous." A slot is only inherited from `SessionState` when the message is a genuine continuation — it references prior context ("same as before", "those", "explain that") or is shaped like a bare continuation ("by district instead", "only for 2024"). This asymmetry matters: it's what stops the agent from silently answering an unrelated bare "revenue" with leftover filters from three questions ago, while still letting genuine follow-ups inherit context naturally.

Session state itself lives behind `BaseSessionStore` (`app/session/store.py`), with two interchangeable backends: `InMemorySessionStore` (LRU-bounded, TTL eviction, correct for one replica) and `RedisSessionStore` (JSON-serialized, shared across replicas — set `SESSION_BACKEND=redis`). History is capped at `session_max_turns` (default 10) turns per session.

## SQL Generation and the AST Guardrail

**`MockLLMProvider.generate_sql`** builds the SELECT string from the resolved query: picks the right semantic-layer tier, builds the metric expression (`SUM`, `AVG`, `COUNT`), assembles `WHERE`/`GROUP BY`/`ORDER BY`/`LIMIT` from the resolved filters, dimensions, years and top-N. Two details worth knowing if you're reading query output:

- **Multi-year comparisons always add `sale_year` to `GROUP BY`.** This was a real, user-reported bug: comparing 2019 and 2020 by district originally grouped only by district, so both years were silently summed into one ambiguous column. Now `sale_year` is appended to the GROUP BY whenever 2+ years are being compared, so every row is unambiguously tied to one year.
- **Entity × time results are capped to a top-N "series limit"** (`series_limit_for`) via a correlated subquery, so "revenue per district per month" doesn't try to chart 64 districts × 12 months as illegible spaghetti — it charts the top 4–8 districts by total, and says so in both the narrative and the chart title.

**Whatever produces this SQL string is never trusted.** `app/security/ast_guardrail.py`'s `validate_and_reserialize_sql` is the actual security boundary, and it runs on *every* query regardless of which provider generated it:

1. **Shape check** — the statement must be a `SELECT` (or a `WITH` whose final projection is one). This alone rejects `DROP`/`INSERT`/`UPDATE`/`DELETE`/`COPY` outright.
2. **Full-tree walk** — catches the same forbidden statement types nested inside a CTE (e.g. `WITH x AS (DELETE ... RETURNING *) SELECT * FROM x`), validates every table reference against an allow-list of exactly two tables (`public.mv_sales_analysis`, `public.mv_sales_daily_rollup`), and validates every function call against an allow-list of ten functions (`SUM`, `AVG`, `COUNT`, `MIN`, `MAX`, `ROUND`, `COALESCE`, `NULLIF`, `CAST`, `LOWER`, `UPPER`).
3. **Row limit enforcement** — caps or injects `LIMIT 500` if the query doesn't already have a safe one.

The validated AST is then **re-serialized back into a SQL string** — the guardrail never executes the model's original text, only its own re-emitted version of the parsed-and-validated tree. This one detail is what closes off parser-differential attacks: text that `sqlglot` and Postgres might tokenize differently can never reach the database, because what reaches the database was built entirely from the validated AST, not copied from untrusted text.

A data-modification request in plain English ("delete all records for Dhaka") is caught even earlier, before any SQL is generated: `scope.is_write_request()` (`app/agent/scope.py`) pattern-matches write-intent verbs against data nouns and is checked as the *first* routing branch in the orchestrator — ahead of greeting/schema-info handling — so it can never be absorbed as an "answer" to a pending clarification and accidentally run as a read report.

## Narrative and Visualization

**`narrative.py`'s `build_narrative`** turns a result set into prose — deliberately paragraph-shaped, not a bulleted "Metric/Breakdown/Filters" dump, because that read as a raw state dump rather than an answer. It works generically off the *shape* of the result (which columns are numeric vs. categorical), not off structured intent from the SQL step, so it behaves identically whether the SQL came from the mock provider or a real LLM. Every number quoted is `sum()`/`max()`/formatting computed in this module directly from `rows` — never an LLM estimate. It branches by shape: a single scalar total, a top-1 superlative result (names the winner explicitly, not just a bare number), a ranked list (states the leader, the runner-up, and — for 3+ rows — the full range), an entity×time breakdown (calls out the strongest/weakest period), and a multi-year comparison (states each figure with its year explicitly and the overall % change — the fix for the ambiguous "which year does this Total Revenue belong to?" bug, with a 0.05% threshold so near-zero float noise isn't misreported as a real "decrease").

**`visualization.py`'s `build_visualization`** picks a chart type from the same result-set shape:

- **`bar`** — one categorical dimension, ranked by value.
- **`line`** — one time dimension, kept strictly chronological (never re-sorted by value — an earlier bug drew a meaningless zig-zag by ranking a trend line by magnitude).
- **`multi_line`** — a time dimension crossed with a series dimension (e.g. revenue per district per month), pivoted so each series is its own key; capped at 8 series.
- **`grouped_bar`** — a small number of entities (≤12) compared across exactly 2 years, one group of bars per entity.
- **`table`** — two independent non-time dimensions (e.g. division × district), or a hierarchy/hi-cardinality comparison too big to chart cleanly.
- A special case, **entity × month × year**, pivots to one line per (entity, year) pair — color encodes the entity, a dashed stroke marks the earlier year — so "Dhaka 2015 vs Dhaka 2016" sit on the same axes instead of a flat 3-column table. This was the direct fix for a reported bug where an explicit "...using a line chart" request for exactly this shape of data was rendered as a 100-row table instead.

**`apply_chart_preference`** honors an explicit request detected earlier ("as a table", "using a bar chart") whenever the data shape actually supports it, and otherwise keeps the better chart and appends a one-line explanation to the narrative for why (e.g. "a line chart is best for trends over time; this compares separate categories, so I kept bars").

## The SSE API Contract

`POST /api/query` (`backend/app/main.py`) takes `{question, session_id}` and returns `text/event-stream`. Each frame is `event: <name>\ndata: <json>\n\n` (built by `app/sse/events.py`, schema version `2.1`). A typical successful turn emits, in order:

1. **`status`** — `{stage: "thinking"}`, then later `{stage: "summarizing"}`.
2. **`step`** — one per pipeline stage (`understand`, `context`, `plan`, `sql`, `guardrail`, `execute`, `answer`), each emitted once with `status: "running"` when it starts and again with `status: "done"` (or `"warning"`/`"error"`) and `elapsed_ms` when it finishes. This is the live reasoning trace.
3. **`sql`** — the guardrail-validated SQL that actually ran, plus whether the result was truncated at the 500-row cap.
4. **`narrative_delta`** — the already-computed narrative text, sent out in small word-chunks with a configurable delay (`narrative_stream_delay_ms`, default 18ms) purely for the "typing" visual effect — the text itself was fully computed before streaming began, so this never blocks on anything real.
5. **`visualization`** — the chart/table config.
6. **`suggestions`** — up to 3 context-aware follow-up questions, phrased so they resolve correctly through `resolve_with_session` if clicked.
7. **`metadata`** — timing breakdown (`llm_ms`/`guardrail_ms`/`sql_ms`/`total_ms`), row count, trace id, last semantic-layer refresh time.
8. **`done`** — a fully consolidated response carrying everything above in one object, for any client that doesn't want to assemble state incrementally from the individual frames.

Other endpoints: `GET /livez` (process up), `GET /healthz` (process up *and* the database answers — used for container health checks and compose `depends_on: condition: service_healthy`), `GET /api/capabilities` (data year range + example questions, shown on an empty chat), `GET /api/session/{id}/history` (rehydrates a reloaded page from the session store), `DELETE /api/session/{id}` ("New chat").

**Robustness details worth knowing:** an `: heartbeat\n\n` SSE comment is sent every 8 seconds of no other activity, so proxies/browsers never mistake a slow LLM call for a dead connection; a client disconnect (or pressing Stop) cancels the in-flight orchestrator task via `finally: task.cancel()` rather than letting it keep burning LLM/DB time; and a per-client-IP token-bucket rate limiter (`app/ratelimit.py`, 10 requests, refilling at 10/minute by default) guards against runaway spend, since each turn can trigger multiple LLM calls via the self-correction retry loop.

## Frontend Architecture

**`ChatStream.tsx`** is the orchestration hub — essentially all client state lives in one component. Per-turn state (`Turn`) tracks the question, live reasoning steps, streamed narrative text, chart config, SQL, and status flags. `send()` opens the SSE stream via `streamQuery` (`lib/sseClient.ts`) and, frame by frame, patches the matching turn's state — a `step` frame upserts into `steps` by id (so a "running" step becomes "done" in place), a `narrative_delta` frame appends text, and so on.

`lib/sseClient.ts` reads the stream manually via `fetch` + `ReadableStream` rather than the browser `EventSource` API, because `EventSource` can't send a POST body (the question has to go in the request body, not a URL query string). It buffers bytes, splits on `\n\n` frame boundaries, and parses `event:`/`data:` lines — silently skipping heartbeat comment lines, which carry no `event:` line at all.

**Session persistence:** the session id lives in `sessionStorage` (survives a reload of the same tab, not a new tab), while the *actual* conversation state lives server-side in the session store — on mount, `fetchHistory()` rehydrates the visible chat from there, so the frontend is never the source of truth. Pinned questions persist in `localStorage` separately, capped at 10.

**The "Thinking..." panel** (`ThinkingPanel.tsx`) auto-expands while a turn is in progress (shimmering header, a live elapsed-time counter) and auto-collapses to "Thought for 1.4s" once the answer starts streaming — but only on that transition, so a user who manually re-opens it afterward isn't fought by the component. Every line in it comes from a real backend `step` event; nothing is simulated client-side.

**`Markdown.tsx`** is a minimal, hand-rolled, XSS-safe renderer (paragraphs, ` -  ` bullets, `**bold**`) built entirely from React nodes, never `dangerouslySetInnerHTML` — so nothing the model or the data could produce can inject markup. It also renders the blinking streaming caret on the last block while a narrative is still arriving.

**`ChartRenderer.tsx`** renders the visualization config with Recharts (bar/line/multi-line/grouped-bar), plus a chart↔table toggle, dark-mode-aware colors (via a `MutationObserver` on `data-theme`, since Recharts applies SVG presentation attributes that don't resolve CSS variables reliably), and a `ChangeCell` that colors year-over-year % change green/red.

**A UI detail directly requested during this engagement:** a clarifying question is visually distinguished from a normal answer — `.assistant-row.is-clarification` in `globals.css` indents the response card and gives it a soft accent background and border, so a follow-up question reads differently at a glance from a completed answer, rather than looking identical to one.

**`next.config.mjs`** proxies `/api/*` to the backend server-side via `rewrites()`, so the browser only ever talks to one origin — this eliminates CORS entirely rather than working around it, and was the fix for an `ERR_BLOCKED_BY_CLIENT` issue hit during browser-based verification. `compress: false` is deliberate: gzip would buffer the whole SSE stream, defeating the token-by-token "typing" effect.

## Testing and Evaluation

**`backend/tests/`** (pytest, async fixtures, a real Postgres — not a mocked one):

- `test_ast_guardrail.py` — the guardrail's allow/deny boundaries directly.
- `test_named_filters.py` — named-value filter extraction against real enum values.
- `test_narrative.py` — narrative text generation across result shapes.
- `test_orchestrator_mock.py` — the orchestrator end to end with `MockLLMProvider`.
- `test_session_and_scope.py` — `SessionState` JSON round-tripping and the `scope.py` helpers in isolation.
- `test_conversation_flow.py` — the largest and most important file: it **replays the actual conversations from the screenshots reported during this engagement** as regression tests (`TestScreenshotConversations`, `TestClarificationSlotFilling`, `TestOutOfScope`, `TestUserReportRound3`, etc.), so the exact "2020" loop, the delete-refusal case, the single-top superlative case, and the line-chart case can never silently regress.
- `test_refresh_scheduler.py`, `test_gemini_provider.py` — the refresh job and the Gemini provider's reply-parsing logic (unit-testable without a live API call).

Run with `make test` (or `pytest` inside `backend/`, with the DB available).

**`eval/eval_harness.py`** computes **Execution Accuracy (EX)**: for each of 30 hand-authored questions in `eval/golden_set.jsonl`, it runs the question through the real orchestrator (real SQL generation, real guardrail, real execution — not mocked) and compares the *result set* against an independently-written ground-truth query, run through the same guardrail. Ground truth is allowed to differ from the generated SQL's exact wording — only the output is compared — except where the two must agree by construction (an "average" question must hit the row-level tier, or it's answering a different question). `make eval` runs it against the mock provider (100% as of the last run in this session); it exits non-zero below a 90% threshold, gating CI (`.github/workflows/`).

Building the golden set — specifically, writing ground-truth SQL independently rather than trusting whatever the mock produced — is what surfaced the three real bugs described earlier in this guide: the non-deterministic float `SUM()`, the "country" contains "count" substring-match bug, and "payment" beating the more specific "bank" on keyword-order ambiguity. All three now have dedicated regression tests, not just a fix left undocumented.

## Running and Configuring the Project

**Quickest path:** `make setup` (creates `.env` from `.env.example` and Docker secret files from templates), then `make up` — brings up Postgres, the backend, and the frontend. Visit the frontend at `http://localhost:3000`. `make down` / `make restart` / `make logs` manage the stack; `make db-shell-agent` opens a `psql` shell as the read-only `agent_ro` role, useful for sanity-checking what the agent itself can see.

**Key environment variables** (`.env`, see `.env.example` for the full list with comments):

- `LLM_PROVIDER` — `mock` (default, no key needed) or `gemini` (needs `GEMINI_API_KEY`, and the backend image must be built with `BACKEND_EXTRAS=redis,gemini` since `google-adk` is an opt-in dependency, not installed by default).
- `SESSION_BACKEND` — `memory` (default, single replica) or `redis` (`docker compose --profile redis up -d`, needed once you run more than one backend replica).
- `NLU_FALLBACK` — `none` (default) or `openai_compatible`, to let an LLM rewrite phrasings the deterministic mapper doesn't recognize into its vocabulary before falling back to "I can't answer that." Works with a local, free Ollama model (`NLU_BASE_URL=http://host.docker.internal:11434/v1`, `NLU_MODEL=qwen2.5:3b`) or the free tiers of Groq/Gemini/OpenRouter — no paid key required either way. This path never generates SQL or numbers, only a rewritten question text that's fed back through the same deterministic pipeline.
- `REFRESH_INTERVAL_MINUTES` (default 60) — how often the materialized views refresh.

**Testing/eval commands:** `make test` (pytest), `make test-slow` (includes the materialized view refresh integration test), `make eval` (Execution Accuracy against the mock provider), `make eval-gemini` (same, against a real Gemini key).

**A note on switching to Gemini:** the provider is fully wired (`gemini_provider.py`) but has not been exercised against a live API call in this engagement — there's no paid key available in this environment. The call shape (Google ADK's `LlmAgent`/`Runner`/`InMemorySessionService`) was verified against the actually-installed package's real signatures, and a critical parameter-mismatch bug found during this review (`_run_single_turn` was being called with a `Runner` object where a role string was expected) is now fixed. Treat the first real call against a live key as the actual integration test for that path — the orchestrator, guardrail, and the rest of the pipeline around it are already proven independent of which provider is active, via `MockLLMProvider`.

## Known Limitations and Where to Extend

These are disclosed, intentional scope boundaries — not oversights — documented here so they're easy to find rather than discovered by surprise:

- **"Semantic understanding" is a hand-maintained synonym table, not real NLU.** `MockLLMProvider` will not understand a genuinely novel phrasing it has no synonym entry for; it will ask a clarifying question or say it's out of scope rather than guess. The optional `NLU_FALLBACK` path extends coverage for unusual wording without weakening this guarantee (see above), and the `GeminiADKProvider` path exists for a fully model-driven alternative — but the deterministic path is, by design, always available and always free.
- **No customer, profit/cost, staff, inventory, forecast, or promotion data exists in the semantic layer.** This isn't a missing feature to add — it's a boundary the dataset doesn't cross, and the agent is designed to say so explicitly (`scope.detect_unsupported`) and offer the closest answerable substitute, rather than silently answering a different question.
- **The evaluation golden set has 30 questions, not a larger number.** The harness, methodology, and CI gate are complete; growing coverage is mechanical (author more `{question, ground_truth_sql}` pairs following the existing pattern in `eval/golden_set.jsonl`) rather than a design gap.
- **"Per store" is approximated by district**, the finest store-location level the semantic layer actually carries (individual store IDs aren't part of it) — the agent states this assumption in its answer rather than silently substituting a different granularity.
- **The rate limiter is in-process memory**, real protection for a single self-hosted deployment but not a substitute for real API-gateway rate limiting at real production scale with multiple replicas.

**Natural next steps for someone picking this up:** exercise `GeminiADKProvider` against a real API key as its first live integration test; grow the golden set past 30 questions; and, if multi-replica deployment is a real near-term need, turn on `SESSION_BACKEND=redis` and load-test the rate limiter's per-process assumption specifically.
