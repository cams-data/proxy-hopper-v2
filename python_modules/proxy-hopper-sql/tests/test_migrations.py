"""Alembic migration tests — schema creation, round-trip, multi-head guard.

Uses a temp *file* per test, not sqlite's :memory:, since async connection
pooling makes :memory: behave inconsistently across connections (each
connection would see its own private empty database).
"""

from __future__ import annotations

import asyncio

from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine

from proxy_hopper_sql import migrations

_EXPECTED_COLUMNS = {
    "entity_type", "name", "data", "static", "mutable", "updated_at", "source_file",
}


def _url(tmp_path, name: str = "config.db") -> str:
    return f"sqlite+aiosqlite:///{tmp_path / name}"


async def _columns(url: str) -> set[str]:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            return await conn.run_sync(
                lambda sync_conn: {
                    col["name"] for col in inspect(sync_conn).get_columns("config_entities")
                }
            )
    finally:
        await engine.dispose()


class TestUpgrade:
    def test_upgrade_creates_table_with_expected_columns(self, tmp_path):
        url = _url(tmp_path)
        migrations.upgrade(url)
        assert asyncio.run(_columns(url)) == _EXPECTED_COLUMNS


class TestRoundTrip:
    def test_upgrade_downgrade_upgrade(self, tmp_path):
        url = _url(tmp_path, "roundtrip.db")

        migrations.upgrade(url)
        migrations.downgrade(url)
        migrations.upgrade(url)

        assert asyncio.run(_columns(url)) == _EXPECTED_COLUMNS


class TestMultiHeadGuard:
    def test_exactly_one_head(self, tmp_path):
        url = _url(tmp_path, "heads.db")
        assert len(migrations.heads(url)) == 1
