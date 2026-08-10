-- Loads the real exported dataset (1,000,000 fact rows) via server-side program pipe,
-- so only gzipped CSVs need to be committed to the repo (see db/seed/, db/README.md).
\copy core.customer_dim FROM PROGRAM 'gunzip -c /seed/core_customer_dim.csv.gz' WITH (FORMAT csv, HEADER true)
\copy core.item_dim     FROM PROGRAM 'gunzip -c /seed/core_item_dim.csv.gz'     WITH (FORMAT csv, HEADER true)
\copy core.store_dim    FROM PROGRAM 'gunzip -c /seed/core_store_dim.csv.gz'    WITH (FORMAT csv, HEADER true)
\copy core.payment_dim  FROM PROGRAM 'gunzip -c /seed/core_payment_dim.csv.gz'  WITH (FORMAT csv, HEADER true)
\copy core.time_dim     FROM PROGRAM 'gunzip -c /seed/core_time_dim.csv.gz'     WITH (FORMAT csv, HEADER true)
\copy core.fact_table(payment_key, customer_key, time_key, item_key, store_key, quantity, unit, unit_price, total_price) FROM PROGRAM 'gunzip -c /seed/core_fact_table.csv.gz' WITH (FORMAT csv, HEADER true)

ANALYZE core.customer_dim;
ANALYZE core.item_dim;
ANALYZE core.store_dim;
ANALYZE core.payment_dim;
ANALYZE core.time_dim;
ANALYZE core.fact_table;
