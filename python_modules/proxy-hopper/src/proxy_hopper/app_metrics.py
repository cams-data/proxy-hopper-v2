"""Lightweight per-target request metrics, stored in the shared Backend.

This is a small, cheap alternative to Prometheus for the admin UI's
per-target metrics panel — total/success/failed request counts, a rolling
average latency, and a last-request timestamp. It is not a replacement for
Prometheus: there are no labels, no histograms, no retention policy, just
four numbers and a timestamp per target.

Because it's stored in the same Backend used for pool/identity state, it
works everywhere the admin API sees live state at all — Redis backend (any
topology), or the memory backend when the admin server is embedded in the
same process as the proxy (``proxy-hopper run --admin``). It does *not*
work with a separately-run admin process on the memory backend, for the
same reason nothing else does: that process has its own private,
disconnected backend.

When ``server.prometheusUrl`` is configured, recording is skipped entirely
(see ``cli.py``) — the admin API queries Prometheus server-side instead, and
paying for both would be pure overhead on the request hot path for no
benefit.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .backend.base import Backend

_PREFIX = "ph"


def _total_key(target: str) -> str:
    return f"{_PREFIX}:{target}:appmetrics:total"


def _success_key(target: str) -> str:
    return f"{_PREFIX}:{target}:appmetrics:success"


def _failed_key(target: str) -> str:
    return f"{_PREFIX}:{target}:appmetrics:failed"


def _latency_sum_key(target: str) -> str:
    return f"{_PREFIX}:{target}:appmetrics:latency_sum_ms"


def _last_request_key(target: str) -> str:
    return f"{_PREFIX}:{target}:appmetrics:last_request_at"


@dataclass(frozen=True)
class TargetMetricsSnapshot:
    """A point-in-time read of one target's app-level metrics."""

    name: str
    total_requests: int
    success_requests: int
    failed_requests: int
    avg_latency_ms: float
    last_request_at: Optional[str]  # ISO-8601 UTC, or None if never recorded


class AppMetricsStore:
    """Records and reads per-target request counters in the shared Backend.

    One instance is shared across all TargetManagers in a process — the
    target name is part of every key, so a single store covers every target.
    """

    def __init__(self, backend: "Backend") -> None:
        self._backend = backend

    async def record(self, target: str, *, success: bool, elapsed_seconds: float) -> None:
        """Record one completed request for *target*.

        Mirrors the same call site and semantics as the Prometheus
        ``proxy_hopper_requests_total``/``proxy_hopper_request_duration_seconds``
        instrumentation (see ``metrics.py``) — one call per upstream attempt,
        including retries, not one call per client-facing request.
        """
        await self._backend.counter_increment(_total_key(target))
        await self._backend.counter_increment(
            _success_key(target) if success else _failed_key(target)
        )
        elapsed_ms = max(0, round(elapsed_seconds * 1000))
        await self._backend.counter_increment_by(_latency_sum_key(target), elapsed_ms)
        await self._backend.kv_set(_last_request_key(target), datetime.now(UTC).isoformat())

    async def get(self, target: str) -> TargetMetricsSnapshot:
        """Return the current snapshot for *target* (all-zero if never recorded)."""
        total = await self._backend.counter_get(_total_key(target))
        success = await self._backend.counter_get(_success_key(target))
        failed = await self._backend.counter_get(_failed_key(target))
        latency_sum_ms = await self._backend.counter_get(_latency_sum_key(target))
        last_request_at = await self._backend.kv_get(_last_request_key(target))
        avg_latency_ms = (latency_sum_ms / total) if total else 0.0
        return TargetMetricsSnapshot(
            name=target,
            total_requests=total,
            success_requests=success,
            failed_requests=failed,
            avg_latency_ms=avg_latency_ms,
            last_request_at=last_request_at,
        )
