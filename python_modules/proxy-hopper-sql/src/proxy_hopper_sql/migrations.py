"""Programmatic Alembic entry points — wraps alembic.command, not the CLI binary.

Used by both the `migrate` click command (for operators / the Docker image)
and the Helm migration Job/initContainer (which calls the same `migrate`
command inside the container).
"""

from __future__ import annotations

import asyncio
import zlib
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

_SCRIPT_LOCATION = Path(__file__).parent / "alembic"

# Arbitrary stable key for pg_advisory_lock — scoped to "this app's config
# migrations", not tied to any particular revision. Any two `migrate`
# invocations against the same Postgres database serialize on this lock,
# regardless of which revision each one is trying to reach.
_ADVISORY_LOCK_KEY = zlib.crc32(b"proxy-hopper-config-store-migrate")


def _config(url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(_SCRIPT_LOCATION))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def _is_postgres(url: str) -> bool:
    return url.startswith("postgresql")


def upgrade(url: str, revision: str = "head") -> None:
    """Apply migrations up to *revision*.

    On Postgres, wrapped in `pg_advisory_lock` so concurrent callers (e.g.
    multiple pods racing the same Helm migration Job) serialize instead of
    racing each other. Not needed for SQLite — a single-writer local file
    already serializes.
    """
    from alembic import command

    if _is_postgres(url):
        asyncio.run(_upgrade_with_advisory_lock(url, revision))
    else:
        command.upgrade(_config(url), revision)


async def _upgrade_with_advisory_lock(url: str, revision: str) -> None:
    from alembic import command

    conn = await _connect_asyncpg(url)
    try:
        await conn.execute("SELECT pg_advisory_lock($1)", _ADVISORY_LOCK_KEY)
        # command.upgrade() is sync and internally does its own asyncio.run()
        # (via env.py) — can't call it directly from inside this coroutine's
        # already-running loop, so push it to a thread instead.
        await asyncio.to_thread(command.upgrade, _config(url), revision)
    finally:
        await conn.execute("SELECT pg_advisory_unlock($1)", _ADVISORY_LOCK_KEY)
        await conn.close()


async def _connect_asyncpg(url: str):
    import asyncpg

    # asyncpg.connect() wants a plain postgresql:// DSN, not the
    # SQLAlchemy-style postgresql+asyncpg:// URL.
    dsn = url.replace("postgresql+asyncpg://", "postgresql://", 1)
    return await asyncpg.connect(dsn)


def downgrade(url: str, revision: str = "base") -> None:
    from alembic import command

    command.downgrade(_config(url), revision)


def heads(url: str) -> list[str]:
    """Return the current head revision(s) — more than one means a branch
    point that needs merging (the CI multi-head guard checks this)."""
    script = ScriptDirectory.from_config(_config(url))
    return list(script.get_heads())
