"""pg_advisory_lock serialization test — requires a real Postgres.

Only runs when POSTGRES_URL is set (a real service container in CI);
skipped locally without one, same as the redis backend's real_redis-marked
tests. SQLite doesn't need this — a single-writer local file already
serializes concurrent migrate calls without any extra locking.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from proxy_hopper_sql import migrations

_POSTGRES_URL = os.environ.get("POSTGRES_URL", "")

pytestmark = pytest.mark.skipif(
    not _POSTGRES_URL, reason="requires POSTGRES_URL (real Postgres)"
)


async def _reset_schema(url: str) -> None:
    """Force a truly fresh DB regardless of what other CI steps already
    migrated on this shared service container — otherwise the race below
    would just see "already at head" and never exercise the lock."""
    import asyncpg

    dsn = url.replace("postgresql+asyncpg://", "postgresql://", 1)
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute("DROP TABLE IF EXISTS config_entities")
        await conn.execute("DROP TABLE IF EXISTS alembic_version")
    finally:
        await conn.close()


class TestConcurrentMigrate:
    def test_concurrent_upgrade_does_not_error_or_double_apply(self):
        asyncio.run(_reset_schema(_POSTGRES_URL))

        # Two independent threads, each running migrations.upgrade() in its
        # own event loop (mirrors two separate processes/pods racing the
        # same Helm migration Job against one Postgres) — without the
        # pg_advisory_lock wrapping, this reliably fails with a duplicate
        # "relation config_entities already exists" error on a fresh DB.
        async def _race() -> None:
            await asyncio.gather(
                asyncio.to_thread(migrations.upgrade, _POSTGRES_URL),
                asyncio.to_thread(migrations.upgrade, _POSTGRES_URL),
            )

        asyncio.run(_race())

        assert len(migrations.heads(_POSTGRES_URL)) == 1
