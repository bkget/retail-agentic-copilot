-- Normalized 3NF source schema, mirrors the real dataset this project was built from.
-- This schema is intentionally NOT exposed to the agent (see 03_roles.sql).
CREATE SCHEMA IF NOT EXISTS core;

CREATE TABLE core.customer_dim (
    customer_key varchar(50) PRIMARY KEY,
    name         varchar(50),
    contact_no   bigint,
    nid          bigint
);

CREATE TABLE core.item_dim (
    item_key    varchar(50) PRIMARY KEY,
    item_name   varchar(50),
    "desc"      varchar(50),
    unit_price  real,
    man_country varchar(50),
    supplier    varchar(50),
    unit        varchar(50)
);

CREATE TABLE core.store_dim (
    store_key varchar(50) PRIMARY KEY,
    division  varchar(50),
    district  varchar(50),
    upazila   varchar(50)
);

CREATE TABLE core.payment_dim (
    payment_key varchar(50) PRIMARY KEY,
    trans_type  varchar(50),
    bank_name   varchar(50)
);

CREATE TABLE core.time_dim (
    time_key varchar(50) PRIMARY KEY,
    date     varchar(50), -- source format: DD-MM-YYYY HH24:MI, parsed to real dates in the semantic layer (see 04_semantic_views.sql)
    hour     integer,
    day      integer,
    week     varchar(50),
    month    integer,
    quarter  varchar(50),
    year     integer
);

CREATE TABLE core.fact_table (
    fact_id      bigserial PRIMARY KEY, -- surrogate key; source data has none, needed for a collision-free semantic-layer key
    payment_key  varchar(50) REFERENCES core.payment_dim(payment_key),
    customer_key varchar(50) REFERENCES core.customer_dim(customer_key),
    time_key     varchar(50) REFERENCES core.time_dim(time_key),
    item_key     varchar(50) REFERENCES core.item_dim(item_key),
    store_key    varchar(50) REFERENCES core.store_dim(store_key),
    quantity     integer,
    unit         varchar(50),
    unit_price   real,
    total_price  real
);

CREATE INDEX idx_fact_customer_key ON core.fact_table(customer_key);
CREATE INDEX idx_fact_item_key ON core.fact_table(item_key);
CREATE INDEX idx_fact_store_key ON core.fact_table(store_key);
CREATE INDEX idx_fact_payment_key ON core.fact_table(payment_key);
CREATE INDEX idx_fact_time_key ON core.fact_table(time_key);
