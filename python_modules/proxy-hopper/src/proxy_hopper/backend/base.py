"""Backend ABC — storage primitives, zero business logic.

The backend abstracts the underlying storage engine (asyncio in-process vs
Redis).  It knows nothing about:
  - Quarantine thresholds or cooldowns          → IPPool / IPPoolStore
  - What keys mean (target names, IP addresses) → IPPoolStore / DynamicConfigStore
  - Configuration objects                        → DynamicConfigStore

Primitive groups
----------------
  Lifecycle         start / stop
  Init lock         claim_init  — first-caller-wins (SETNX semantics)
  Queue             queue_push / queue_push_many / queue_pop_blocking / queue_size
  Counter           counter_increment / counter_set / counter_get
  Sorted set        sorted_set_add / sorted_set_pop_by_max_score / sorted_set_members
  Compound read     queue_size_and_sorted_set_members — single round trip where possible
  Key-value         kv_set / kv_get / kv_delete / kv_list
  Pub/sub           publish / subscribe

Implementations
---------------
MemoryBackend    — asyncio queues + dicts; single-process only
RedisBackend     — Redis lists, sorted sets, strings, pub/sub; HA multi-instance
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional


class Backend(ABC):
    """Storage primitives for all persistent proxy-hopper state.

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
    # Init lock — first-caller-wins across all instances
    # ------------------------------------------------------------------

    @abstractmethod
    async def claim_init(self, key: str) -> bool:
        """Atomically claim initialisation ownership for *key*.

        Returns True for the first caller; False for all subsequent callers,
        even across multiple processes/instances.

        Memory: set-membership check (always unique per process).
        Redis:  SETNX with a 24-hour TTL so a full Redis flush allows
                re-seeding without a process restart.
        """

    # ------------------------------------------------------------------
    # Queue — ordered FIFO, blocking pop
    # ------------------------------------------------------------------

    @abstractmethod
    async def queue_push(self, key: str, value: str) -> None:
        """Append *value* to the tail of the queue at *key*."""

    async def queue_push_many(self, key: str, values: list[str]) -> None:
        """Append multiple values in one operation.

        Default: sequential calls to queue_push.
        Backends may override for a single-round-trip bulk insert (e.g. Redis RPUSH).
        """
        for value in values:
            await self.queue_push(key, value)

    @abstractmethod
    async def queue_pop_blocking(self, key: str, timeout: float) -> Optional[str]:
        """Remove and return the head of the queue, blocking up to *timeout* seconds.

        Returns None on timeout.

        Memory: asyncio.wait_for on Queue.get().
        Redis:  BLPOP with float timeout (sub-second precision, Redis ≥ 6.0).
        """

    @abstractmethod
    async def queue_size(self, key: str) -> int:
        """Return the number of items currently in the queue."""

    # ------------------------------------------------------------------
    # Counter — atomic increment / set / get
    # ------------------------------------------------------------------

    @abstractmethod
    async def counter_increment(self, key: str) -> int:
        """Atomically increment the counter at *key* and return the new value.

        Counter is initialised to 0 if it does not exist.
        Memory: no-await dict update (atomic in asyncio).
        Redis:  INCR (atomic).
        """

    @abstractmethod
    async def counter_set(self, key: str, value: int) -> None:
        """Set the counter at *key* to *value*."""

    @abstractmethod
    async def counter_get(self, key: str) -> int:
        """Return the current counter value (0 if key does not exist)."""

    # ------------------------------------------------------------------
    # Sorted set — score-ordered, atomic pop below threshold
    # ------------------------------------------------------------------

    @abstractmethod
    async def sorted_set_add(self, key: str, member: str, score: float) -> None:
        """Add *member* with *score* to the sorted set at *key*.

        If *member* already exists its score is updated.
        """

    @abstractmethod
    async def sorted_set_pop_by_max_score(
        self, key: str, max_score: float
    ) -> list[str]:
        """Atomically claim and remove all members with score <= *max_score*.

        Each member is returned by exactly one caller even under concurrent
        access from multiple coroutines or instances.

        Memory: single-pass dict comprehension + deletion (asyncio-atomic).
        Redis:  Lua script — ZRANGEBYSCORE + ZREM in one server-side call,
                eliminating the race window and reducing N round trips to 1.
        """

    @abstractmethod
    async def sorted_set_members(self, key: str) -> list[str]:
        """Return all current members of the sorted set (for status/metrics)."""

    # ------------------------------------------------------------------
    # Compound read — queue size + sorted set members in one shot
    # ------------------------------------------------------------------

    async def queue_size_and_sorted_set_members(
        self, queue_key: str, set_key: str
    ) -> tuple[int, list[str]]:
        """Return (queue_size, sorted_set_members) for the given keys.

        Default: two sequential calls.
        Redis backend overrides this with a single pipelined round trip.
        """
        return await self.queue_size(queue_key), await self.sorted_set_members(set_key)

    # ------------------------------------------------------------------
    # Key-value store — for dynamic config persistence
    # ------------------------------------------------------------------

    @abstractmethod
    async def kv_set(self, key: str, value: str) -> None:
        """Store a string *value* under *key*, overwriting any existing value."""

    @abstractmethod
    async def kv_get(self, key: str) -> Optional[str]:
        """Return the value stored at *key*, or None if the key does not exist."""

    @abstractmethod
    async def kv_delete(self, key: str) -> None:
        """Delete *key*. No-op if the key does not exist."""

    @abstractmethod
    async def kv_list(self, prefix: str) -> list[tuple[str, str]]:
        """Return all (key, value) pairs whose key starts with *prefix*."""

    # ------------------------------------------------------------------
    # Pub/sub — lightweight change notification
    # ------------------------------------------------------------------

    @abstractmethod
    async def publish(self, channel: str, message: str) -> None:
        """Publish *message* to *channel*.

        All active subscribers on *channel* receive the message.
        """

    @abstractmethod
    def subscribe(self, channel: str) -> "AsyncIteratorContextManager":
        """Return an async context manager that yields messages from *channel*.

        Usage::

            async with backend.subscribe("my-channel") as messages:
                async for msg in messages:
                    handle(msg)

        The subscription is cleaned up when the context manager exits.
        """


class AsyncIteratorContextManager:
    """Type alias hint — an object usable as both async context manager and iterator."""


# ---------------------------------------------------------------------------
# Backward-compatible aliases
# ---------------------------------------------------------------------------

#: Deprecated alias — use Backend directly.
IPPoolBackend = Backend
