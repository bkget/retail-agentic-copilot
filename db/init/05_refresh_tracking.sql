-- Tracks materialized view freshness so the API can populate `last_refreshed_at`
-- (see backend/app/refresh/scheduler.py, which calls refresh_semantic_views() on a timer).

CREATE TABLE public.refresh_log (
    view_name    text PRIMARY KEY,
    refreshed_at timestamptz NOT NULL,
    duration_ms  integer NOT NULL
);

INSERT INTO public.refresh_log (view_name, refreshed_at, duration_ms) VALUES
    ('public.mv_sales_analysis', now(), 0),
    ('public.mv_sales_daily_rollup', now(), 0);

ALTER TABLE public.refresh_log OWNER TO refresher_rw;
GRANT SELECT ON public.refresh_log TO agent_ro;

-- Order matters: mv_sales_daily_rollup's defining query selects FROM mv_sales_analysis,
-- so it must be refreshed second to pick up the new data.
CREATE FUNCTION public.refresh_semantic_views() RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE
    t0 timestamptz;
BEGIN
    t0 := clock_timestamp();
    REFRESH MATERIALIZED VIEW CONCURRENTLY public.mv_sales_analysis;
    INSERT INTO public.refresh_log (view_name, refreshed_at, duration_ms)
    VALUES ('public.mv_sales_analysis', clock_timestamp(), EXTRACT(MILLISECONDS FROM clock_timestamp() - t0))
    ON CONFLICT (view_name) DO UPDATE SET refreshed_at = EXCLUDED.refreshed_at, duration_ms = EXCLUDED.duration_ms;

    t0 := clock_timestamp();
    REFRESH MATERIALIZED VIEW CONCURRENTLY public.mv_sales_daily_rollup;
    INSERT INTO public.refresh_log (view_name, refreshed_at, duration_ms)
    VALUES ('public.mv_sales_daily_rollup', clock_timestamp(), EXTRACT(MILLISECONDS FROM clock_timestamp() - t0))
    ON CONFLICT (view_name) DO UPDATE SET refreshed_at = EXCLUDED.refreshed_at, duration_ms = EXCLUDED.duration_ms;
END;
$$;

ALTER FUNCTION public.refresh_semantic_views() OWNER TO refresher_rw;
