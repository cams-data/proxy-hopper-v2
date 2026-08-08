"""SqlConfigStore — SQLAlchemy-backed ConfigStore, SQLite or Postgres.

Dialect is disambiguated entirely by the connection URL's scheme
(``sqlite+aiosqlite://`` vs ``postgresql+asyncpg://``) — no separate
"store type" field, mirroring how ``redis_url`` alone already selects the
Redis backend elsewhere in this codebase.

Schema is expected to already exist (via the ``migrate`` CLI / Helm
migration job) — start() does not create tables.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import delete as sa_delete
from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from proxy_hopper.config_store.base import ConfigEntity, ConfigStore

from .schema import config_entities


class SqlConfigStore(ConfigStore):
    """ConfigStore backed by a SQLAlchemy async engine."""

    def __init__(self, url: str) -> None:
        self._url = url
        self._engine: AsyncEngine | None = None

    async def start(self) -> None:
        self._engine = create_async_engine(self._url)

    async def stop(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None

    async def get(self, entity_type: str, name: str) -> ConfigEntity | None:
        async with self._engine.connect() as conn:
            result = await conn.execute(
                select(config_entities).where(
                    config_entities.c.entity_type == entity_type,
                    config_entities.c.name == name,
                )
            )
            row = result.first()
            return self._to_entity(row) if row is not None else None

    async def set(
        self,
        entity_type: str,
        name: str,
        data: dict,
        *,
        static: bool,
        mutable: bool,
        source_file: Optional[str] = None,
    ) -> None:
        # Naive UTC — SQLite's DATETIME type doesn't reliably round-trip
        # tz-aware datetimes, so ConfigEntity.updated_at is naive UTC by
        # convention across every ConfigStore implementation.
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        async with self._engine.begin() as conn:
            result = await conn.execute(
                select(config_entities.c.name).where(
                    config_entities.c.entity_type == entity_type,
                    config_entities.c.name == name,
                )
            )
            if result.first() is not None:
                await conn.execute(
                    update(config_entities)
                    .where(
                        config_entities.c.entity_type == entity_type,
                        config_entities.c.name == name,
                    )
                    .values(
                        data=data, static=static, mutable=mutable,
                        updated_at=now, source_file=source_file,
                    )
                )
            else:
                await conn.execute(
                    insert(config_entities).values(
                        entity_type=entity_type,
                        name=name,
                        data=data,
                        static=static,
                        mutable=mutable,
                        updated_at=now,
                        source_file=source_file,
                    )
                )

    async def delete(self, entity_type: str, name: str) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                sa_delete(config_entities).where(
                    config_entities.c.entity_type == entity_type,
                    config_entities.c.name == name,
                )
            )

    async def list(self, entity_type: str) -> list[ConfigEntity]:
        async with self._engine.connect() as conn:
            result = await conn.execute(
                select(config_entities).where(
                    config_entities.c.entity_type == entity_type
                )
            )
            return [self._to_entity(row) for row in result]

    @staticmethod
    def _to_entity(row) -> ConfigEntity:
        return ConfigEntity(
            name=row.name,
            data=row.data,
            static=row.static,
            mutable=row.mutable,
            updated_at=row.updated_at,
            source_file=row.source_file,
        )
