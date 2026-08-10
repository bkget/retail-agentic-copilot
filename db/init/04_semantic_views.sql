-- Semantic layer exposed to the agent. Two tiers:
--   Tier 1 (mv_sales_daily_rollup): pre-aggregated, the default for most analytical questions.
--   Tier 2 (mv_sales_analysis):     row-level OBT for drill-downs, PII excluded entirely
--                                    (no customer_key/name/contact - there is no analytical
--                                    need for customer-level rows in this project's scope).
--
-- Corrections applied here vs. the original spec/legacy view (see plan doc for rationale):
--   - sale_date is a real `date` (source core.time_dim.date is text 'DD-MM-YYYY HH24:MI').
--   - payment_bank uses NULLIF to turn the literal string 'None' into real SQL NULL.
--   - fact_key is the fact table's own surrogate PK, not a hash (hashing item/store/payment/
--     time was not collision-free against the real data - 2 collisions in 1M rows).
--   - total_price/unit_price are cast to numeric, not left as the source `real` (float4).
--     Verified empirically (see eval/README.md): the exact same SUM(total_price) query run
--     twice back-to-back returned DIFFERENT totals (e.g. 40764592.0 vs 40764620.0) because
--     Postgres's parallel aggregation combines per-worker partial sums in a non-fixed order,
--     and float4 addition isn't associative - accumulated rounding error depends on
--     summation order, which varies run to run. `numeric` addition is exact, so this
--     wasn't a rounding-precision nice-to-have, it was a correctness bug directly
--     undermining the "no math hallucinations" goal: two runs of the identical question
--     could show two different revenue totals to the same user.

CREATE MATERIALIZED VIEW public.mv_sales_analysis AS
SELECT
    f.fact_id                                    AS fact_key,
    f.total_price::numeric,
    f.quantity,
    f.unit_price::numeric,
    i.item_name,
    i.supplier                                   AS item_supplier,
    i.man_country                                 AS item_manufacturer_country,
    s.division                                    AS store_division,
    s.district                                    AS store_district,
    s.upazila                                     AS store_upazila,
    p.trans_type                                  AS payment_type,
    NULLIF(p.bank_name, 'None')                   AS payment_bank,
    to_date(t.date, 'DD-MM-YYYY HH24:MI')         AS sale_date,
    t.year                                        AS sale_year,
    t.quarter                                     AS sale_quarter,
    t.month                                       AS sale_month,
    t.day                                         AS sale_day
FROM core.fact_table f
INNER JOIN core.item_dim i     ON f.item_key = i.item_key
INNER JOIN core.store_dim s    ON f.store_key = s.store_key
INNER JOIN core.payment_dim p  ON f.payment_key = p.payment_key
INNER JOIN core.time_dim t     ON f.time_key = t.time_key
WITH DATA;

CREATE UNIQUE INDEX idx_mv_sales_fact_key ON public.mv_sales_analysis (fact_key);
CREATE INDEX idx_mv_sales_date ON public.mv_sales_analysis (sale_date);
CREATE INDEX idx_mv_sales_location ON public.mv_sales_analysis (store_division, store_district);
CREATE INDEX idx_mv_sales_item ON public.mv_sales_analysis (item_name);

CREATE MATERIALIZED VIEW public.mv_sales_daily_rollup AS
SELECT
    sale_date,
    sale_year,
    sale_month,
    store_division,
    store_district,
    item_name,
    SUM(total_price)   AS total_revenue,
    SUM(quantity)       AS total_units_sold,
    COUNT(*)             AS transaction_count
FROM public.mv_sales_analysis
GROUP BY 1, 2, 3, 4, 5, 6
WITH DATA;

CREATE UNIQUE INDEX idx_mv_rollup_grain ON public.mv_sales_daily_rollup (sale_date, store_division, store_district, item_name);
CREATE INDEX idx_mv_rollup_date ON public.mv_sales_daily_rollup (sale_date);
CREATE INDEX idx_mv_rollup_division ON public.mv_sales_daily_rollup (store_division);

-- Postgres only allows the OWNER (or superuser) to REFRESH a materialized view - there is
-- no separate grantable "refresh" privilege. Transfer ownership to refresher_rw so it can
-- refresh without being a superuser. CONCURRENTLY requires the unique indexes above.
-- Grants (below) are independent of ownership and are unaffected by this transfer.
ALTER MATERIALIZED VIEW public.mv_sales_analysis OWNER TO refresher_rw;
ALTER MATERIALIZED VIEW public.mv_sales_daily_rollup OWNER TO refresher_rw;

-- REFRESH re-executes the view's defining query, so the owner also needs SELECT on
-- whatever that query reads - in this case the core.* tables mv_sales_analysis joins.
-- Deliberately excludes core.customer_dim: mv_sales_analysis never references it, so
-- refresher_rw has no reason to be able to read customer PII either.
GRANT SELECT ON core.fact_table, core.item_dim, core.store_dim, core.payment_dim, core.time_dim TO refresher_rw;

-- agent_ro sees only these two objects - the entirety of the semantic layer it can query.
GRANT SELECT ON public.mv_sales_analysis TO agent_ro;
GRANT SELECT ON public.mv_sales_daily_rollup TO agent_ro;
