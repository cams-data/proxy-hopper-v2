"""ConfigStore ABC — durable config storage, zero business logic.

Splits durable config (providers/pools/targets created or edited via the
admin API) off the operational-state `Backend`. `Backend` keeps doing
exactly what it does today for queues/counters/quarantine — nothing in
this module is a replacement for that.

Implementations
---------------
MemoryConfigStore — dict-of-dicts; test-only, not a real deployment option
                    (see CONFIG_STORE_SCOPE.md's design decisions table).
SqlConfigStore     — SQLite/Postgres via SQLAlchemy (proxy-hopper-sql, later phase).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class ConfigEntity:
    """A single stored provider/pool/target row.

    `static` and `mutable` mirror the same flags already carried on
    `ProxyProvider`/`IpPool`/`TargetConfig` — static means YAML-owned and
    API-immutable, mutable means the admin API can edit or delete it.
    """

    name: str
    data: dict
    static: bool
    mutable: bool
    # Naive UTC by convention across every implementation — SQLite's
    # DATETIME type doesn't reliably round-trip tz-aware datetimes.
    updated_at: datetime
    # Root-relative path of the file that produced this entity, e.g.
    # "providers/aws.yaml" — set by ProxyRepository.reconcile() for
    # file-owned (static) entities so conflict/duplicate messages can name
    # the actual file. None for admin-API-created entities and any entity
    # that predates this field.
    source_file: Optional[str] = None


class ConfigStore(ABC):
    """Durable storage for provider/pool/target config.

    `entity_type` is one of "provider" | "pool" | "target" throughout —
    the same three kinds `ProxyRepository` already handles.
    """

    @abstractmethod
    async def start(self) -> None:
        """Initialise the store (connections, schema checks, etc.)."""

    @abstractmethod
    async def stop(self) -> None:
        """Release resources gracefully."""

    @abstractmethod
    async def get(self, entity_type: str, name: str) -> ConfigEntity | None:
        """Return the entity named *name*, or None if it does not exist."""

    @abstractmethod
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
        """Create or overwrite the entity named *name*.

        Overwrites `data`, `static`, `mutable`, `source_file`, and
        `updated_at` in full — this is not a partial update. `source_file`
        defaults to None so every pre-existing call site (admin-API CRUD)
        needs no change; only ProxyRepository.reconcile() passes it.
        """

    @abstractmethod
    async def delete(self, entity_type: str, name: str) -> None:
        """Delete the entity named *name*. No-op if it does not exist."""

    @abstractmethod
    async def list(self, entity_type: str) -> list[ConfigEntity]:
        """Return all entities of *entity_type*, empty list if none exist."""
