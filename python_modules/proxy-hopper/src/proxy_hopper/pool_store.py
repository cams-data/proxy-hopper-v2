"""IPPoolStore — domain wrapper around Backend for IP pool and identity state.

Owns all key naming for pool-related state and translates pool-domain
operations into generic Backend primitives.

Key schema
----------
ph:{target}:pool                  — Queue      — UUID strings (identity IDs)
ph:{target}:identity:{uuid}       — KV         — JSON-serialised Identity data
ph:{target}:ip:{address}          — KV         — active UUID for this address
ph:{target}:retired:{address}     — KV         — "1" if address is retired
ph:{target}:init                  — Init       — SETNX claim for first-start seeding
ph:{target}:failures:{addr}       — Counter    — consecutive failure count for *addr*
ph:{target}:quarantine            — Sorted set — member=address, score=release_epoch

This class is not a Backend subclass — it holds a Backend and delegates.
IdentityQueue imports IPPoolStore and passes it to the backend as a typed dependency.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from .backend.base import Backend

logger = logging.getLogger(__name__)

_PREFIX = "ph"


def _pool_key(target: str) -> str:
    return f"{_PREFIX}:{target}:pool"


def _identity_key(target: str, uuid: str) -> str:
    return f"{_PREFIX}:{target}:identity:{uuid}"


def _ip_key(target: str, address: str) -> str:
    return f"{_PREFIX}:{target}:ip:{address}"


def _retired_key(target: str, address: str) -> str:
    return f"{_PREFIX}:{target}:retired:{address}"


def _init_key(target: str) -> str:
    return f"{_PREFIX}:{target}:init"


def _failures_key(target: str, address: str) -> str:
    return f"{_PREFIX}:{target}:failures:{address}"


def _quarantine_key(target: str) -> str:
    return f"{_PREFIX}:{target}:quarantine"


class IPPoolStore:
    """Pool-domain operations backed by a generic Backend."""

    def __init__(self, backend: Backend) -> None:
        self._backend = backend

    # ------------------------------------------------------------------
    # Init race — first-caller-wins across all instances
    # ------------------------------------------------------------------

    async def claim_init(self, target: str) -> bool:
        """Return True iff this caller wins the init race for *target*."""
        return await self._backend.claim_init(_init_key(target))

    # ------------------------------------------------------------------
    # Pool queue — stores identity UUID strings
    # ------------------------------------------------------------------

    async def push_identity_uuid(self, target: str, uuid: str) -> None:
        """Push an identity UUID to the tail of the pool queue."""
        await self._backend.queue_push(_pool_key(target), uuid)

    async def pop_identity_uuid(self, target: str, timeout: float) -> Optional[str]:
        """Pop an identity UUID from the head of the pool queue (blocking)."""
        return await self._backend.queue_pop_blocking(_pool_key(target), timeout)

    async def pool_size(self, target: str) -> int:
        return await self._backend.queue_size(_pool_key(target))

    # ------------------------------------------------------------------
    # Identity KV — full identity JSON stored per UUID
    # ------------------------------------------------------------------

    async def identity_write(self, target: str, uuid: str, data: dict) -> None:
        """Serialise and store *data* as the identity for *uuid*."""
        await self._backend.kv_set(_identity_key(target, uuid), json.dumps(data))

    async def identity_read(self, target: str, uuid: str) -> Optional[dict]:
        """Return the deserialised identity dict for *uuid*, or None if missing."""
        raw = await self._backend.kv_get(_identity_key(target, uuid))
        if raw is None:
            return None
        return json.loads(raw)

    async def identity_delete(self, target: str, uuid: str) -> None:
        """Delete the identity KV entry for *uuid*."""
        await self._backend.kv_delete(_identity_key(target, uuid))

    # ------------------------------------------------------------------
    # IP → UUID reverse lookup — one active UUID per address
    # ------------------------------------------------------------------

    async def ip_set(self, target: str, address: str, uuid: str) -> None:
        """Record that *uuid* is the active identity for *address*."""
        await self._backend.kv_set(_ip_key(target, address), uuid)

    async def ip_delete(self, target: str, address: str) -> None:
        """Remove the active UUID record for *address*."""
        await self._backend.kv_delete(_ip_key(target, address))

    # ------------------------------------------------------------------
    # Retired address set — addresses pending discard
    # ------------------------------------------------------------------

    async def retire_add(self, target: str, address: str) -> None:
        """Mark *address* as retired.  Its identity will be discarded on next pop."""
        await self._backend.kv_set(_retired_key(target, address), "1")

    async def retire_check(self, target: str, address: str) -> bool:
        """Return True if *address* has been marked as retired."""
        return await self._backend.kv_get(_retired_key(target, address)) is not None

    async def retire_remove(self, target: str, address: str) -> None:
        """Remove the retired marker for *address* (called after discard)."""
        await self._backend.kv_delete(_retired_key(target, address))

    # ------------------------------------------------------------------
    # Failure counter
    # ------------------------------------------------------------------

    async def increment_failures(self, target: str, address: str) -> int:
        return await self._backend.counter_increment(_failures_key(target, address))

    async def reset_failures(self, target: str, address: str) -> None:
        await self._backend.counter_set(_failures_key(target, address), 0)

    async def get_failures(self, target: str, address: str) -> int:
        return await self._backend.counter_get(_failures_key(target, address))

    # ------------------------------------------------------------------
    # Quarantine sorted set
    # ------------------------------------------------------------------

    async def quarantine_add(
        self, target: str, address: str, release_at: float
    ) -> None:
        await self._backend.sorted_set_add(_quarantine_key(target), address, release_at)

    async def quarantine_pop_expired(self, target: str, now: float) -> list[str]:
        return await self._backend.sorted_set_pop_by_max_score(
            _quarantine_key(target), now
        )

    async def quarantine_list(self, target: str) -> list[str]:
        return await self._backend.sorted_set_members(_quarantine_key(target))

    # ------------------------------------------------------------------
    # Compound read — single round trip where possible
    # ------------------------------------------------------------------

    async def pool_size_and_quarantine(self, target: str) -> tuple[int, list[str]]:
        """Return (pool_size, quarantined_addresses) in one backend call."""
        return await self._backend.queue_size_and_sorted_set_members(
            _pool_key(target), _quarantine_key(target)
        )
