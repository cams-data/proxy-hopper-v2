"""IPPoolStore — domain wrapper around Backend for IP pool state.

Owns all key naming for pool-related state and translates pool-domain
operations (target + address) into generic Backend primitives.

Key schema
----------
ph:{target}:pool             — Queue  — available IP addresses ("host:port")
ph:{target}:init             — Init   — SETNX claim for first-start seeding
ph:{target}:failures:{addr}  — Counter — consecutive failure count for *addr*
ph:{target}:quarantine       — Sorted set — member=address, score=release_epoch

This class is not a Backend subclass — it holds a Backend and delegates.
IPPool imports IPPoolStore and passes it to the backend as a typed dependency.
"""

from __future__ import annotations

import logging
from typing import Optional

from .backend.base import Backend

logger = logging.getLogger(__name__)

_PREFIX = "ph"


def _pool_key(target: str) -> str:
    return f"{_PREFIX}:{target}:pool"


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
    # Pool queue
    # ------------------------------------------------------------------

    async def push_ip(self, target: str, address: str) -> None:
        await self._backend.queue_push(_pool_key(target), address)

    async def push_ips(self, target: str, addresses: list[str]) -> None:
        await self._backend.queue_push_many(_pool_key(target), addresses)

    async def pop_ip(self, target: str, timeout: float) -> Optional[str]:
        return await self._backend.queue_pop_blocking(_pool_key(target), timeout)

    async def pool_size(self, target: str) -> int:
        return await self._backend.queue_size(_pool_key(target))

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
