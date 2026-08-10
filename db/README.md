# Database layer

## What's here

- `init/` — mounted into the Postgres container at `/docker-entrypoint-initdb.d`. Files run
  once, in filename order, only on first container start (empty `pgdata` volume):
  1. `01_schema_core.sql` — normalized 3NF source schema (`core.*`), never exposed to the agent.
  2. `02_load_core_data.sql` — loads the real seed dataset via `\copy ... FROM PROGRAM 'gunzip -c ...'`.
  3. `03_roles.sh` — creates `agent_ro` (query path, read-only, hardened session limits) and
     `refresher_rw` (owns the materialized views, refreshes them on a schedule). Passwords come
     from env vars / docker secrets, never hardcoded.
  4. `04_semantic_views.sql` — the two-tier semantic layer (`mv_sales_analysis`,
     `mv_sales_daily_rollup`) the agent actually queries.
  5. `05_refresh_tracking.sql` — `refresh_log` table + `refresh_semantic_views()` function.
- `seed/*.csv.gz` — gzipped exports of `core.*` from the original dataset (1,000,000 fact rows).

## Where the seed data came from

Exported once from a local Postgres instance that already had this exact schema populated
(bank/e-commerce style synthetic-but-realistic Bangladesh retail data). To regenerate from a
differently-hosted source with the same schema:

```bash
for t in customer_dim item_dim store_dim payment_dim time_dim fact_table; do
  psql "$SOURCE_CONN" -c "\copy core.$t TO STDOUT WITH CSV HEADER" | gzip -9 > db/seed/core_${t}.csv.gz
done
```

## Why `fact_key` isn't a hash

The original spec hashed `customer_key|item_key|store_key|payment_key|time_key` for a PII-safe
row identifier. Two problems with that against the real data: (1) it embeds `customer_key`,
which conflicts with excluding customer data from the semantic layer entirely, and (2) hashing
just `item_key|store_key|payment_key|time_key` collides twice in the real 1M-row dataset — not
suitable as a unique key. `core.fact_table` now has its own `bigserial` surrogate PK
(`fact_id`), which `mv_sales_analysis.fact_key` uses directly. It carries no customer
information and is guaranteed unique.

## Why `total_price`/`unit_price` are cast to `numeric`

The source `core.fact_table` stores these as `real` (float4), which `mv_sales_analysis`
initially passed through unchanged. Building the eval harness (see `eval/README.md`)
caught this empirically: the *identical* `SUM(total_price) ... GROUP BY store_division`
query, run twice back-to-back, returned two different totals for the same division/year
(e.g. `40764592.0` vs `40764620.0`). Cause: Postgres's parallel aggregation combines
per-worker partial sums in a run-dependent order, and float4 addition isn't associative,
so accumulated rounding error varies run to run. `numeric` addition is exact, so casting
`total_price`/`unit_price` to `numeric` in `04_semantic_views.sql` makes `SUM()`
deterministic regardless of parallel execution plan. This wasn't a precision nicety - an
agent whose stated goal is "no math hallucinations" showing two different answers to the
identical question is exactly the failure mode that goal is supposed to prevent.

## Known data quality notes (intentionally not silently "fixed")

- `item_dim.man_country` has inconsistent casing (`poland` vs `Netherlands`). Left as-is in the
  semantic layer — normalizing it would mask genuinely dirty source data that's more useful
  surfaced (e.g. in eval questions) than hidden.
