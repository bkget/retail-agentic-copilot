"""Integration test fixtures. These tests exercise the real semantic layer, so they
require `docker compose up -d retail_copilot_db` to be running first (see repo README). They
connect through the same `agent_ro` role and the same secret files the app itself uses -
no credentials are duplicated/hardcoded here.
"""

from __future__ import annotations

import os
from pathlib import Path

import asyncpg
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

os.environ.setdefault("POSTGRES_HOST", "localhost")
os.environ.setdefault("POSTGRES_PORT", "5433")  # host-mapped port from docker-compose.yml
os.environ.setdefault(
    "POSTGRES_AGENT_PASSWORD_FILE", str(REPO_ROOT / "secrets" / "agent_password.txt")
)
os.environ.setdefault(
    "POSTGRES_REFRESHER_PASSWORD_FILE", str(REPO_ROOT / "secrets" / "refresher_password.txt")
)

from app.config import get_settings  # noqa: E402  (must follow env var setup above)


@pytest.fixture(scope="session")
def settings():
    return get_settings()


@pytest.fixture
async def agent_pool(settings):
    pool = await asyncpg.create_pool(dsn=settings.agent_dsn(), min_size=1, max_size=2)
    yield pool
    await pool.close()


@pytest.fixture
async def refresher_pool(settings):
    pool = await asyncpg.create_pool(dsn=settings.refresher_dsn(), min_size=1, max_size=1)
    yield pool
    await pool.close()
