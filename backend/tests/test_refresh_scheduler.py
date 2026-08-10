"""Integration test for the actual refresh path (not just that it's wired up - that it
executes real SQL against the real 1M-row views and updates refresh_log). This is slow
(~30-60s: REFRESH MATERIALIZED VIEW CONCURRENTLY on the full dataset) - kept to a single
test deliberately rather than parametrizing, to avoid paying that cost repeatedly.
"""

from __future__ import annotations

import datetime

import pytest

from app.refresh.scheduler import RefreshScheduler

pytestmark = pytest.mark.slow


async def test_run_refresh_updates_refresh_log_and_calls_callback(refresher_pool):
    async with refresher_pool.acquire() as conn:
        before = {
            r["view_name"]: r["refreshed_at"]
            for r in await conn.fetch("SELECT view_name, refreshed_at FROM public.refresh_log")
        }

    callback_calls = []

    async def on_refreshed():
        callback_calls.append(datetime.datetime.now(datetime.timezone.utc))

    scheduler = RefreshScheduler(refresher_pool, interval_minutes=60, on_refreshed=on_refreshed)
    await scheduler._run_refresh()

    assert len(callback_calls) == 1

    async with refresher_pool.acquire() as conn:
        after = {
            r["view_name"]: r["refreshed_at"]
            for r in await conn.fetch("SELECT view_name, refreshed_at FROM public.refresh_log")
        }

    for view_name, before_ts in before.items():
        assert after[view_name] > before_ts
