"""Abstract backend interface — pure storage primitives, zero business logic.

The backend knows nothing about:
  - Quarantine thresholds (that is config / IPPool logic)
  - Cooldown intervals (same)
  - When to retry or fail a request (TargetManager / IPPool logic)

It only knows how to operate on its underlying storage (asyncio queues,
dicts, Redis lists, sorted sets, etc.).  Every method maps 1-to-1 with a
concrete storage operation.

Implementations
---------------
MemoryIPPoolBackend  — asyncio queues + plain dicts; single-process only.
RedisIPPoolBackend   — Redis List / Hash / Sorted Set; multi-instance HA.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional


class IPPoolBackend(ABC):
    """Storage primitives for IP pool management.

    All methods are async to accommodate both local (asyncio) and remote
    (Redis) implementations under the same interface.
    """

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @abstractmethod
    async def start(self) -> None:
        """Initialise the backend (connections, background tasks, etc.)."""

    @abstractmethod
    async def stop(self) -> None:
        """Release resources gracefully."""

    # ------------------------------------------------------------------
    # Target initialisation
    # ------------------------------------------------------------------

    @abstractmethod
    async def init_target(self, target: str) -> bool:
        """Claim initialisation ownership for *target*.

        Returns True if the caller should seed the IP pool (i.e. this is the
        first call for this target in this storage scope).

        Memory backend: always True — fresh process state.
        Redis backend:  uses SETNX — True only on the first call across all
                        running instances.
        """

    # ------------------------------------------------------------------
    # IP pool queue
    # ------------------------------------------------------------------

    @abstractmethod
    async def push_ip(self, target: str, address: str) -> None:
        """Add *address* to the tail of the available IP pool for *target*."""

    @abstractmethod
    async def pop_ip(self, target: str, timeout: float) -> Optional[str]:
        """Remove and return the next available IP for *target*.

        Blocks up to *timeout* seconds.  Returns None on timeout.
        """

    @abstractmethod
    async def pool_size(self, target: str) -> int:
        """Return the number of IPs currently in the available pool."""

    # ------------------------------------------------------------------
    # Failure counter (per IP per target)
    # ------------------------------------------------------------------

    @abstractmethod
    async def increment_failures(self, target: str, address: str) -> int:
        """Atomically increment and return the new consecutive failure count."""

    @abstractmethod
    async def reset_failures(self, target: str, address: str) -> None:
        """Reset the consecutive failure counter to zero."""

    @abstractmethod
    async def get_failures(self, target: str, address: str) -> int:
        """Return the current consecutive failure count (0 if never set)."""

    # ------------------------------------------------------------------
    # Quarantine sorted set
    # ------------------------------------------------------------------

    @abstractmethod
    async def quarantine_add(
        self, target: str, address: str, release_at: float
    ) -> None:
        """Add *address* to quarantine with a release epoch timestamp as score."""

    @abstractmethod
    async def quarantine_pop_expired(
        self, target: str, now: float
    ) -> list[str]:
        """Atomically claim all quarantine entries whose release time <= *now*.

        Each expired entry is returned by exactly one caller even when called
        concurrently from multiple coroutines or processes.  Entries that
        another caller has already claimed are silently skipped.
        """

    @abstractmethod
    async def quarantine_list(self, target: str) -> list[str]:
        """Return all currently quarantined addresses (for status / metrics)."""

    async def pool_size_and_quarantine(self, target: str) -> tuple[int, list[str]]:
        """Return (pool_size, quarantine_list) in one operation.

        Default implementation calls both methods sequentially.
        Backends may override to fetch both in a single round trip.
        """
        return await self.pool_size(target), await self.quarantine_list(target)

    # ------------------------------------------------------------------
    # Bulk operations
    # ------------------------------------------------------------------

    async def push_ips(self, target: str, addresses: list[str]) -> None:
        """Add multiple addresses to the pool in one operation.

        Default implementation calls push_ip sequentially.
        Backends may override for a single-round-trip bulk insert.
        """
        for address in addresses:
            await self.push_ip(target, address)
