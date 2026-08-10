"""Periodically refreshes the semantic-layer materialized views. Runs on the
`refresher_rw` connection pool - never the `agent_ro` pool the query path uses, so a
compromised query path can never trigger (or interfere with) a refresh.

The first refresh is scheduled one interval out, not immediately at startup: the DB
init scripts (db/init/05_refresh_tracking.sql) already seed refresh_log with a fresh
timestamp, so an immediate refresh on every container start would just be redundant
work against 1M rows for no benefit.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

import asyncpg
from apscheduler.schedulers.asyncio import AsyncIOScheduler

logger = logging.getLogger(__name__)

JOB_ID = "refresh_semantic_views"


class RefreshScheduler:
    def __init__(
        self,
        refresher_pool: asyncpg.Pool,
        interval_minutes: int,
        on_refreshed: Callable[[], Awaitable[None]] | None = None,
    ):
        self._pool = refresher_pool
        self._interval_minutes = interval_minutes
        self._on_refreshed = on_refreshed
        self._scheduler = AsyncIOScheduler()

    def start(self) -> None:
        self._scheduler.add_job(
            self._run_refresh,
            trigger="interval",
            minutes=self._interval_minutes,
            id=JOB_ID,
            max_instances=1,  # a slow refresh must never overlap with the next tick
            coalesce=True,
        )
        self._scheduler.start()

    def shutdown(self) -> None:
        self._scheduler.shutdown(wait=False)

    async def _run_refresh(self) -> None:
        try:
            async with self._pool.acquire() as conn:
                await conn.execute("SELECT public.refresh_semantic_views()")
            logger.info("Semantic view refresh completed")
        except Exception:
            logger.exception("Semantic view refresh failed - will retry next interval")
            return

        if self._on_refreshed is not None:
            try:
                await self._on_refreshed()
            except Exception:
                logger.exception("Post-refresh callback failed")
