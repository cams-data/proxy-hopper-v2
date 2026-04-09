"""In-process asyncio backend — pure storage primitives, no business logic.

Data structures per target
--------------------------
_pools       : dict[target, asyncio.Queue[str]]   — available IP addresses
_failures    : dict[target, dict[address, int]]   — consecutive failure counts
_quarantine  : dict[target, dict[address, float]] — address → release epoch

Thread / concurrency safety
---------------------------
asyncio is single-threaded with cooperative multitasking.  All methods that
mutate internal dicts do so without yielding (no ``await`` between read and
write), which makes them effectively atomic for asyncio consumers.

The only actual async operation is Queue.get() with a timeout, which yields
to the event loop to wait for an item to become available.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from .base import IPPoolBackend

logger = logging.getLogger(__name__)


class MemoryIPPoolBackend(IPPoolBackend):
    """Asyncio-queue / dict backend for single-instance deployments."""

    def __init__(self) -> None:
        self._pools: dict[str, asyncio.Queue[str]] = {}
        self._failures: dict[str, dict[str, int]] = {}
        self._quarantine: dict[str, dict[str, float]] = {}
        self._initialised: set[str] = set()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        logger.debug("MemoryIPPoolBackend: started")

    async def stop(self) -> None:
        logger.debug("MemoryIPPoolBackend: stopped")

    # ------------------------------------------------------------------
    # Target initialisation
    # ------------------------------------------------------------------

    async def init_target(self, target: str) -> bool:
        if target in self._initialised:
            logger.trace(  # type: ignore[attr-defined]
                "MemoryIPPoolBackend: init_target '%s' → already initialised", target
            )
            return False
        self._initialised.add(target)
        self._pools[target] = asyncio.Queue()
        self._failures[target] = {}
        self._quarantine[target] = {}
        logger.trace(  # type: ignore[attr-defined]
            "MemoryIPPoolBackend: init_target '%s' → initialised", target
        )
        return True

    # ------------------------------------------------------------------
    # IP pool queue
    # ------------------------------------------------------------------

    async def push_ip(self, target: str, address: str) -> None:
        await self._pools[target].put(address)
        logger.trace(  # type: ignore[attr-defined]
            "MemoryIPPoolBackend: push_ip '%s' %s (queue size: %d)",
            target, address, self._pools[target].qsize(),
        )

    async def pop_ip(self, target: str, timeout: float) -> Optional[str]:
        try:
            result = await asyncio.wait_for(self._pools[target].get(), timeout=timeout)
            logger.trace(  # type: ignore[attr-defined]
                "MemoryIPPoolBackend: pop_ip '%s' → %s", target, result
            )
            return result
        except (asyncio.TimeoutError, TimeoutError):
            logger.trace(  # type: ignore[attr-defined]
                "MemoryIPPoolBackend: pop_ip '%s' → timeout after %.2fs", target, timeout
            )
            return None

    async def pool_size(self, target: str) -> int:
        return self._pools[target].qsize()

    # ------------------------------------------------------------------
    # Failure counter
    # ------------------------------------------------------------------

    async def increment_failures(self, target: str, address: str) -> int:
        # No await between read and write — atomically safe in asyncio
        current = self._failures[target].get(address, 0)
        self._failures[target][address] = current + 1
        logger.trace(  # type: ignore[attr-defined]
            "MemoryIPPoolBackend: increment_failures '%s' %s → %d",
            target, address, current + 1,
        )
        return current + 1

    async def reset_failures(self, target: str, address: str) -> None:
        self._failures[target][address] = 0
        logger.trace(  # type: ignore[attr-defined]
            "MemoryIPPoolBackend: reset_failures '%s' %s", target, address
        )

    async def get_failures(self, target: str, address: str) -> int:
        return self._failures[target].get(address, 0)

    # ------------------------------------------------------------------
    # Quarantine sorted set
    # ------------------------------------------------------------------

    async def quarantine_add(
        self, target: str, address: str, release_at: float
    ) -> None:
        self._quarantine[target][address] = release_at
        logger.trace(  # type: ignore[attr-defined]
            "MemoryIPPoolBackend: quarantine_add '%s' %s (release_at=%.3f)",
            target, address, release_at,
        )

    async def quarantine_pop_expired(
        self, target: str, now: float
    ) -> list[str]:
        # Identify and atomically remove in one pass — safe in asyncio
        expired = [
            addr
            for addr, release_at in self._quarantine[target].items()
            if release_at <= now
        ]
        for addr in expired:
            del self._quarantine[target][addr]
        logger.trace(  # type: ignore[attr-defined]
            "MemoryIPPoolBackend: quarantine_pop_expired '%s' → %d expired: %s",
            target, len(expired), expired,
        )
        return expired

    async def quarantine_list(self, target: str) -> list[str]:
        return list(self._quarantine[target].keys())
