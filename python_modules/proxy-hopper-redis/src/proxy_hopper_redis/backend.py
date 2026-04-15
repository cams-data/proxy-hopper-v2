"""Redis-backed storage primitives for the Backend interface.

This class contains only Redis operations — no business logic.  It has no
knowledge of quarantine thresholds, cooldown intervals, or retry counts.
All of that lives in the domain stores (IPPoolStore, DynamicConfigStore).

Redis data structures
---------------------
Queues       — Redis Lists    (RPUSH / BLPOP)
Counters     — Redis Strings  (INCR / SET / GET)
Sorted sets  — Redis ZSets    (ZADD / ZRANGEBYSCORE+ZREM Lua / ZRANGE)
KV store     — Redis Strings  (SET / GET / DEL / SCAN+MGET)
Pub/sub      — Redis Pub/Sub  (PUBLISH / SUBSCRIBE on dedicated connection)
Init lock    — Redis Strings  (SET NX EX)

HA / multi-instance safety
--------------------------
queue_push / queue_pop_blocking
    RPUSH / BLPOP are atomic — each item delivered to exactly one caller.

counter_increment
    INCR is atomic; safe under concurrent access from many instances.

sorted_set_pop_by_max_score
    Lua script: ZRANGEBYSCORE + ZREM in one server-side call, eliminating
    the race window and reducing N round trips to 1.

claim_init
    SET NX EX — atomic acquire + TTL in one round trip.  First caller wins;
    TTL allows re-seeding after a full Redis flush without a process restart.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

from proxy_hopper.backend.base import Backend

try:
    import redis.asyncio as aioredis
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "proxy-hopper-redis requires the 'redis' package. "
        "Install it with: pip install proxy-hopper-redis"
    ) from exc

logger = logging.getLogger(__name__)


# Atomically pop all sorted-set members whose score <= ARGV[1].
# KEYS[1] = sorted set key
# ARGV[1] = max score (float string)
# Returns the list of claimed members.
_SORTED_SET_POP_SCRIPT = """
local members = redis.call('ZRANGEBYSCORE', KEYS[1], 0, ARGV[1])
for _, m in ipairs(members) do
    redis.call('ZREM', KEYS[1], m)
end
return members
"""


class RedisBackend(Backend):
    """Pure Redis storage backend — all operations map to Redis commands."""

    def __init__(self, redis_url: str = "redis://localhost:6379/0") -> None:
        self._redis_url = redis_url
        self._redis: aioredis.Redis | None = None
        self._sorted_set_pop: aioredis.client.Script | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self._redis is None:
            logger.debug("RedisBackend: connecting to %s", self._redis_url)
            self._redis = aioredis.from_url(
                self._redis_url,
                encoding="utf-8",
                decode_responses=True,
            )
        await self._redis.ping()
        self._sorted_set_pop = self._redis.register_script(_SORTED_SET_POP_SCRIPT)
        logger.info("RedisBackend: connected to %s", self._redis_url)

    async def stop(self) -> None:
        if self._redis:
            await self._redis.aclose()
        logger.info("RedisBackend: disconnected")

    # ------------------------------------------------------------------
    # Init lock
    # ------------------------------------------------------------------

    async def claim_init(self, key: str) -> bool:
        """SET NX EX — atomic acquire + 24-hour TTL in one round trip."""
        acquired = await self._redis.set(key, "1", nx=True, ex=86_400)
        logger.trace(  # type: ignore[attr-defined]
            "RedisBackend: claim_init '%s' → %s",
            key, "acquired" if acquired else "already claimed",
        )
        return bool(acquired)

    # ------------------------------------------------------------------
    # Queue (Redis List)
    # ------------------------------------------------------------------

    async def queue_push(self, key: str, value: str) -> None:
        length = await self._redis.rpush(key, value)
        logger.trace(  # type: ignore[attr-defined]
            "RedisBackend: RPUSH '%s' %r (length: %d)", key, value, length
        )

    async def queue_push_many(self, key: str, values: list[str]) -> None:
        if not values:
            return
        length = await self._redis.rpush(key, *values)
        logger.trace(  # type: ignore[attr-defined]
            "RedisBackend: RPUSH '%s' [%d values] (length: %d)",
            key, len(values), length,
        )

    async def queue_pop_blocking(self, key: str, timeout: float) -> Optional[str]:
        """BLPOP with float timeout (sub-second precision, Redis ≥ 6.0)."""
        result = await self._redis.blpop(key, timeout=max(0.1, timeout))
        address = result[1] if result else None
        logger.trace(  # type: ignore[attr-defined]
            "RedisBackend: BLPOP '%s' → %r", key, address
        )
        return address

    async def queue_size(self, key: str) -> int:
        return await self._redis.llen(key)

    # ------------------------------------------------------------------
    # Counter (Redis String / INCR)
    # ------------------------------------------------------------------

    async def counter_increment(self, key: str) -> int:
        count = await self._redis.incr(key)
        logger.trace(  # type: ignore[attr-defined]
            "RedisBackend: INCR '%s' → %d", key, count
        )
        return count

    async def counter_set(self, key: str, value: int) -> None:
        await self._redis.set(key, value)
        logger.trace(  # type: ignore[attr-defined]
            "RedisBackend: SET '%s' %d", key, value
        )

    async def counter_get(self, key: str) -> int:
        value = await self._redis.get(key)
        return int(value) if value is not None else 0

    # ------------------------------------------------------------------
    # Sorted set (Redis ZSet)
    # ------------------------------------------------------------------

    async def sorted_set_add(self, key: str, member: str, score: float) -> None:
        await self._redis.zadd(key, {member: score})
        logger.trace(  # type: ignore[attr-defined]
            "RedisBackend: ZADD '%s' %r score=%.3f", key, member, score
        )

    async def sorted_set_pop_by_max_score(
        self, key: str, max_score: float
    ) -> list[str]:
        """Lua script — ZRANGEBYSCORE + ZREM atomically, single round trip."""
        claimed: list[str] = await self._sorted_set_pop(  # type: ignore[misc]
            keys=[key],
            args=[str(max_score)],
        )
        logger.trace(  # type: ignore[attr-defined]
            "RedisBackend: sorted_set_pop_by_max_score '%s' max=%.3f → %d: %s",
            key, max_score, len(claimed), claimed,
        )
        return claimed

    async def sorted_set_members(self, key: str) -> list[str]:
        return await self._redis.zrange(key, 0, -1)

    # ------------------------------------------------------------------
    # Compound read — single pipeline round trip
    # ------------------------------------------------------------------

    async def queue_size_and_sorted_set_members(
        self, queue_key: str, set_key: str
    ) -> tuple[int, list[str]]:
        """LLEN + ZRANGE in a single pipeline — one round trip."""
        async with self._redis.pipeline(transaction=False) as pipe:
            pipe.llen(queue_key)
            pipe.zrange(set_key, 0, -1)
            size, members = await pipe.execute()
        return int(size), members

    # ------------------------------------------------------------------
    # Key-value store (Redis Strings with prefix scan)
    # ------------------------------------------------------------------

    async def kv_set(self, key: str, value: str) -> None:
        await self._redis.set(key, value)
        logger.trace(  # type: ignore[attr-defined]
            "RedisBackend: SET '%s'", key
        )

    async def kv_get(self, key: str) -> Optional[str]:
        return await self._redis.get(key)

    async def kv_delete(self, key: str) -> None:
        await self._redis.delete(key)
        logger.trace(  # type: ignore[attr-defined]
            "RedisBackend: DEL '%s'", key
        )

    async def kv_list(self, prefix: str) -> list[tuple[str, str]]:
        """SCAN for matching keys then MGET — avoids blocking KEYS in production."""
        pattern = f"{prefix}*"
        keys: list[str] = []
        async for k in self._redis.scan_iter(pattern):
            keys.append(k)
        if not keys:
            return []
        values = await self._redis.mget(*keys)
        return [(k, v) for k, v in zip(keys, values) if v is not None]

    # ------------------------------------------------------------------
    # Pub/sub (dedicated connection per subscribe context)
    # ------------------------------------------------------------------

    async def publish(self, channel: str, message: str) -> None:
        await self._redis.publish(channel, message)
        logger.trace(  # type: ignore[attr-defined]
            "RedisBackend: PUBLISH '%s'", channel
        )

    @asynccontextmanager
    async def subscribe(self, channel: str) -> AsyncIterator[AsyncIterator[str]]:  # type: ignore[override]
        """Open a dedicated pub/sub connection for *channel*.

        Uses a separate Redis connection so SUBSCRIBE does not block the
        main connection's command pipeline.
        """
        pubsub_client = aioredis.from_url(
            self._redis_url,
            encoding="utf-8",
            decode_responses=True,
        )
        pubsub = pubsub_client.pubsub()
        await pubsub.subscribe(channel)
        logger.trace(  # type: ignore[attr-defined]
            "RedisBackend: subscribe '%s'", channel
        )

        async def _iter() -> AsyncIterator[str]:
            async for raw in pubsub.listen():
                if raw["type"] == "message":
                    yield raw["data"]

        try:
            yield _iter()
        finally:
            await pubsub.unsubscribe(channel)
            await pubsub.aclose()
            await pubsub_client.aclose()
            logger.trace(  # type: ignore[attr-defined]
                "RedisBackend: unsubscribe '%s'", channel
            )


# ---------------------------------------------------------------------------
# Backward-compatible alias
# ---------------------------------------------------------------------------

#: Deprecated alias — use RedisBackend directly.
RedisIPPoolBackend = RedisBackend
