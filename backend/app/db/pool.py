from __future__ import annotations

import asyncpg

from app.config import Settings

# Query path never gets more than a handful of concurrent SSE streams in this project's
# scope; small pool keeps behavior predictable under the DB's own connection limits.
AGENT_POOL_MIN_SIZE = 2
AGENT_POOL_MAX_SIZE = 10

# Refresh runs one job at a time on a timer - a single connection is enough.
REFRESHER_POOL_SIZE = 1


async def create_agent_pool(settings: Settings) -> asyncpg.Pool:
    return await asyncpg.create_pool(
        dsn=settings.agent_dsn(),
        min_size=AGENT_POOL_MIN_SIZE,
        max_size=AGENT_POOL_MAX_SIZE,
        command_timeout=10,  # slightly above the DB's own 8s statement_timeout
    )


async def create_refresher_pool(settings: Settings) -> asyncpg.Pool:
    return await asyncpg.create_pool(
        dsn=settings.refresher_dsn(),
        min_size=REFRESHER_POOL_SIZE,
        max_size=REFRESHER_POOL_SIZE,
        command_timeout=120,  # REFRESH MATERIALIZED VIEW on 1M rows can take ~60-90s
    )
