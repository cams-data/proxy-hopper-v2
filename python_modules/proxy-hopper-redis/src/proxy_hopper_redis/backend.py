"""Redis-backed storage primitives for the IPPoolBackend interface.

This class contains only Redis operations — no business logic.  It has no
knowledge of quarantine thresholds, cooldown intervals, or retry counts.
All of that lives in IPPool (proxy_hopper.pool).

Redis data structures
---------------------
ph:{target}:pool         — List  — available IP address strings ("host:port")
ph:{target}:failures:{a} — String (integer) — consecutive failure count for IP a
ph:{target}:quarantine   — ZSet  — member=address, score=release_epoch

HA / multi-instance safety
--------------------------
push_ip / pop_ip
    RPUSH / BLPOP are atomic Redis operations — each IP is delivered to
    exactly one caller.

increment_failures
    INCR is atomic; safe under concurrent access from many instances.

quarantine_pop_expired
    ZRANGEBYSCORE identifies candidates; ZREM atomically claims them.
    Only the instance that wins ZREM for a given member returns it.

init_target
    SETNX on an init-lock key ensures only the first instance to start
    populates the pool.  Subsequent calls from any instance return False.
"""

from __future__ import annotations

import logging
from typing import Optional

from proxy_hopper.backend.base import IPPoolBackend

try:
    import redis.asyncio as aioredis
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "proxy-hopper-redis requires the 'redis' package. "
        "Install it with: pip install proxy-hopper-redis"
    ) from exc

logger = logging.getLogger(__name__)


class RedisIPPoolBackend(IPPoolBackend):
    """Pure Redis storage backend — all operations map 1-to-1 with Redis commands."""

    def __init__(self, redis_url: str = "redis://localhost:6379/0") -> None:
        self._redis_url = redis_url
        self._redis: aioredis.Redis | None = None

    # ------------------------------------------------------------------
    # Redis key helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _pool_key(target: str) -> str:
        return f"ph:{target}:pool"

    @staticmethod
    def _failures_key(target: str, address: str) -> str:
        return f"ph:{target}:failures:{address}"

    @staticmethod
    def _quarantine_key(target: str) -> str:
        return f"ph:{target}:quarantine"

    @staticmethod
    def _init_key(target: str) -> str:
        return f"ph:{target}:initialized"

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self._redis is None:
            logger.debug("RedisIPPoolBackend: connecting to %s", self._redis_url)
            self._redis = aioredis.from_url(
                self._redis_url,
                encoding="utf-8",
                decode_responses=True,
            )
        await self._redis.ping()
        logger.info("RedisIPPoolBackend: connected to %s", self._redis_url)

    async def stop(self) -> None:
        if self._redis:
            await self._redis.aclose()
        logger.info("RedisIPPoolBackend: disconnected")

    # ------------------------------------------------------------------
    # Target initialisation
    # ------------------------------------------------------------------

    async def init_target(self, target: str) -> bool:
        """SETNX — returns True only for the first caller across all instances."""
        acquired = await self._redis.setnx(self._init_key(target), "1")
        if acquired:
            # Short TTL so the pool can be re-seeded after a full Redis flush
            await self._redis.expire(self._init_key(target), 86_400)
        logger.trace(  # type: ignore[attr-defined]
            "RedisIPPoolBackend: SETNX %s → %s",
            self._init_key(target), "acquired" if acquired else "already exists",
        )
        return bool(acquired)

    # ------------------------------------------------------------------
    # IP pool queue  (Redis List)
    # ------------------------------------------------------------------

    async def push_ip(self, target: str, address: str) -> None:
        """RPUSH — append address to the tail of the pool list."""
        length = await self._redis.rpush(self._pool_key(target), address)
        logger.trace(  # type: ignore[attr-defined]
            "RedisIPPoolBackend: RPUSH %s %s (list length: %d)",
            self._pool_key(target), address, length,
        )

    async def pop_ip(self, target: str, timeout: float) -> Optional[str]:
        """BLPOP — atomically pop from the head; returns None on timeout."""
        result = await self._redis.blpop(
            self._pool_key(target), timeout=max(1, int(timeout))
        )
        address = result[1] if result else None
        logger.trace(  # type: ignore[attr-defined]
            "RedisIPPoolBackend: BLPOP %s → %s", self._pool_key(target), address
        )
        return address

    async def pool_size(self, target: str) -> int:
        """LLEN — number of available IPs."""
        return await self._redis.llen(self._pool_key(target))

    # ------------------------------------------------------------------
    # Failure counter  (Redis String / INCR)
    # ------------------------------------------------------------------

    async def increment_failures(self, target: str, address: str) -> int:
        """INCR — atomic increment; returns new value."""
        count = await self._redis.incr(self._failures_key(target, address))
        logger.trace(  # type: ignore[attr-defined]
            "RedisIPPoolBackend: INCR %s → %d",
            self._failures_key(target, address), count,
        )
        return count

    async def reset_failures(self, target: str, address: str) -> None:
        """SET 0 — reset counter."""
        await self._redis.set(self._failures_key(target, address), 0)
        logger.trace(  # type: ignore[attr-defined]
            "RedisIPPoolBackend: SET %s 0", self._failures_key(target, address)
        )

    async def get_failures(self, target: str, address: str) -> int:
        """GET — return current counter (0 if key does not exist)."""
        value = await self._redis.get(self._failures_key(target, address))
        return int(value) if value is not None else 0

    # ------------------------------------------------------------------
    # Quarantine sorted set  (Redis ZSet)
    # ------------------------------------------------------------------

    async def quarantine_add(
        self, target: str, address: str, release_at: float
    ) -> None:
        """ZADD — add address with score = release epoch."""
        await self._redis.zadd(self._quarantine_key(target), {address: release_at})
        logger.trace(  # type: ignore[attr-defined]
            "RedisIPPoolBackend: ZADD %s %s score=%.3f",
            self._quarantine_key(target), address, release_at,
        )

    async def quarantine_pop_expired(
        self, target: str, now: float
    ) -> list[str]:
        """ZRANGEBYSCORE + ZREM — atomically claim expired entries.

        ZREM is atomic per entry: only the instance that removes a member
        gets to process it, preventing double-release under concurrent calls.
        """
        candidates: list[str] = await self._redis.zrangebyscore(
            self._quarantine_key(target), 0, now
        )
        claimed: list[str] = []
        for address in candidates:
            removed = await self._redis.zrem(self._quarantine_key(target), address)
            if removed:
                claimed.append(address)
        logger.trace(  # type: ignore[attr-defined]
            "RedisIPPoolBackend: quarantine_pop_expired '%s' candidates=%d claimed=%d: %s",
            target, len(candidates), len(claimed), claimed,
        )
        return claimed

    async def quarantine_list(self, target: str) -> list[str]:
        """ZRANGE — all currently quarantined addresses (for status/metrics)."""
        return await self._redis.zrange(self._quarantine_key(target), 0, -1)
