"""In-process asyncio backend — pure storage primitives, no business logic.

Data structures
---------------
_queues     : dict[key, asyncio.Queue[str]]        — FIFO queues
_counters   : dict[key, int]                       — atomic counters
_sorted     : dict[key, dict[member, float]]       — sorted sets (member → score)
_kv         : dict[key, str]                       — key-value store
_pubsub     : dict[channel, list[Queue[str]]]      — one Queue per active subscriber
_init_keys  : set[str]                             — claimed init keys

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
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

from .base import Backend
from ..logging_config import get_logger

logger = get_logger(__name__)


class MemoryBackend(Backend):
    """Asyncio-queue / dict backend for single-instance deployments."""

    def __init__(self) -> None:
        self._queues: dict[str, asyncio.Queue[str]] = {}
        self._counters: dict[str, int] = {}
        self._sorted: dict[str, dict[str, float]] = {}
        self._kv: dict[str, str] = {}
        self._pubsub: dict[str, list[asyncio.Queue[str]]] = {}
        self._init_keys: set[str] = set()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        logger.debug("MemoryBackend: started")

    async def stop(self) -> None:
        logger.debug("MemoryBackend: stopped")

    # ------------------------------------------------------------------
    # Init lock
    # ------------------------------------------------------------------

    async def claim_init(self, key: str) -> bool:
        if key in self._init_keys:
            logger.trace(
                "MemoryBackend: claim_init '%s' → already claimed", key
            )
            return False
        self._init_keys.add(key)
        logger.trace(
            "MemoryBackend: claim_init '%s' → claimed", key
        )
        return True

    # ------------------------------------------------------------------
    # Queue
    # ------------------------------------------------------------------

    async def queue_push(self, key: str, value: str) -> None:
        if key not in self._queues:
            self._queues[key] = asyncio.Queue()
        await self._queues[key].put(value)
        logger.trace(
            "MemoryBackend: queue_push '%s' %r (size: %d)",
            key, value, self._queues[key].qsize(),
        )

    async def queue_push_many(self, key: str, values: list[str]) -> None:
        if not values:
            return
        if key not in self._queues:
            self._queues[key] = asyncio.Queue()
        for value in values:
            await self._queues[key].put(value)
        logger.trace(
            "MemoryBackend: queue_push_many '%s' [%d values] (size: %d)",
            key, len(values), self._queues[key].qsize(),
        )

    async def queue_pop_blocking(self, key: str, timeout: float) -> Optional[str]:
        if key not in self._queues:
            self._queues[key] = asyncio.Queue()
        try:
            result = await asyncio.wait_for(self._queues[key].get(), timeout=timeout)
            logger.trace(
                "MemoryBackend: queue_pop_blocking '%s' → %r", key, result
            )
            return result
        except (asyncio.TimeoutError, TimeoutError):
            logger.trace(
                "MemoryBackend: queue_pop_blocking '%s' → timeout after %.2fs", key, timeout
            )
            return None

    async def queue_size(self, key: str) -> int:
        return self._queues[key].qsize() if key in self._queues else 0

    # ------------------------------------------------------------------
    # Counter
    # ------------------------------------------------------------------

    async def counter_increment(self, key: str) -> int:
        # No await between read and write — atomically safe in asyncio
        new = self._counters.get(key, 0) + 1
        self._counters[key] = new
        logger.trace(
            "MemoryBackend: counter_increment '%s' → %d", key, new
        )
        return new

    async def counter_set(self, key: str, value: int) -> None:
        self._counters[key] = value
        logger.trace(
            "MemoryBackend: counter_set '%s' = %d", key, value
        )

    async def counter_get(self, key: str) -> int:
        return self._counters.get(key, 0)

    # ------------------------------------------------------------------
    # Sorted set
    # ------------------------------------------------------------------

    async def sorted_set_add(self, key: str, member: str, score: float) -> None:
        if key not in self._sorted:
            self._sorted[key] = {}
        self._sorted[key][member] = score
        logger.trace(
            "MemoryBackend: sorted_set_add '%s' member=%r score=%.3f", key, member, score
        )

    async def sorted_set_pop_by_max_score(
        self, key: str, max_score: float
    ) -> list[str]:
        if key not in self._sorted:
            return []
        # Identify and remove in one pass — atomically safe in asyncio
        expired = [m for m, s in self._sorted[key].items() if s <= max_score]
        for m in expired:
            del self._sorted[key][m]
        logger.trace(
            "MemoryBackend: sorted_set_pop_by_max_score '%s' max=%.3f → %d: %s",
            key, max_score, len(expired), expired,
        )
        return expired

    async def sorted_set_members(self, key: str) -> list[str]:
        return list(self._sorted[key].keys()) if key in self._sorted else []

    # ------------------------------------------------------------------
    # Key-value store
    # ------------------------------------------------------------------

    async def kv_set(self, key: str, value: str) -> None:
        self._kv[key] = value
        logger.trace(
            "MemoryBackend: kv_set '%s'", key
        )

    async def kv_get(self, key: str) -> Optional[str]:
        return self._kv.get(key)

    async def kv_delete(self, key: str) -> None:
        self._kv.pop(key, None)
        logger.trace(
            "MemoryBackend: kv_delete '%s'", key
        )

    async def kv_list(self, prefix: str) -> list[tuple[str, str]]:
        return [(k, v) for k, v in self._kv.items() if k.startswith(prefix)]

    # ------------------------------------------------------------------
    # Pub/sub
    # ------------------------------------------------------------------

    async def publish(self, channel: str, message: str) -> None:
        subscribers = self._pubsub.get(channel, [])
        for q in subscribers:
            await q.put(message)
        logger.trace(
            "MemoryBackend: publish '%s' → %d subscriber(s)", channel, len(subscribers)
        )

    @asynccontextmanager
    async def subscribe(self, channel: str) -> AsyncIterator[AsyncIterator[str]]:  # type: ignore[override]
        q: asyncio.Queue[str] = asyncio.Queue()
        if channel not in self._pubsub:
            self._pubsub[channel] = []
        self._pubsub[channel].append(q)
        logger.trace(
            "MemoryBackend: subscribe '%s' (now %d subscriber(s))",
            channel, len(self._pubsub[channel]),
        )

        async def _iter() -> AsyncIterator[str]:
            while True:
                yield await q.get()

        try:
            yield _iter()
        finally:
            self._pubsub[channel].remove(q)
            logger.trace(
                "MemoryBackend: unsubscribe '%s' (now %d subscriber(s))",
                channel, len(self._pubsub[channel]),
            )


# ---------------------------------------------------------------------------
# Backward-compatible alias
# ---------------------------------------------------------------------------

#: Deprecated alias — use MemoryBackend directly.
MemoryIPPoolBackend = MemoryBackend
