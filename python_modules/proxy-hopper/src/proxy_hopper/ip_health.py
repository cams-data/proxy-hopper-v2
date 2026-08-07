"""Per-IP reachability status from the background prober, stored in the
shared Backend.

Mirrors ``app_metrics.py``'s ``AppMetricsStore`` — same rationale, same
cross-process characteristics: because it's stored in the same Backend used
for pool/identity state, it's visible everywhere the admin API sees live
state at all (Redis backend, any topology; or the memory backend only when
the admin server is embedded via ``proxy-hopper run --admin``). A
separately-run admin process on the memory backend has no visibility, same
as everything else backed by a private, disconnected backend — configure
``server.prometheusUrl`` in that case instead.

Unlike ``AppMetricsStore``, recording here is never skipped when
``prometheusUrl`` is configured: the prober runs once per probe round (tens
of seconds apart), not once per proxied request, so writing to both is
negligible cost — no hot-path double-instrumentation concern.

One JSON value per address (not one key per field, unlike ``AppMetricsStore``)
so a read via ``kv_list`` returns one parseable row per address directly, and
a single ``kv_set_with_ttl`` call updates all fields atomically.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .backend.base import Backend

_PREFIX = "ph:iphealth:"

# Entries for IPs that are no longer probed (removed from config) expire
# instead of lingering forever — a few probe rounds' worth of grace so a
# transient config-reload gap doesn't flash an IP to "unknown".
_TTL_INTERVAL_MULTIPLIER = 3


def _key(address: str) -> str:
    return f"{_PREFIX}{address}"


@dataclass(frozen=True)
class IpHealthSnapshot:
    """A point-in-time read of one IP's reachability status."""

    address: str
    provider: Optional[str]
    status: Optional[str]  # "up" | "down" | None (never probed / no data)
    last_check_at: Optional[str]  # ISO-8601 UTC, or None if never recorded
    reason: Optional[str]  # failure reason (see prober.py), None if status != "down"


def _unknown(address: str) -> IpHealthSnapshot:
    return IpHealthSnapshot(address=address, provider=None, status=None, last_check_at=None, reason=None)


class IpHealthStore:
    """Records and reads per-IP reachability status in the shared Backend.

    One instance is shared by the prober for writes and by GraphQL resolvers
    for reads — the address is part of every key, so a single store covers
    every probed IP.
    """

    def __init__(self, backend: "Backend", probe_interval: float = 60.0) -> None:
        self._backend = backend
        self._ttl_seconds = max(1, round(probe_interval * _TTL_INTERVAL_MULTIPLIER))

    async def record(
        self,
        address: str,
        *,
        success: bool,
        provider: str = "",
        reason: Optional[str] = None,
    ) -> None:
        """Record one probe result for *address*."""
        value = json.dumps({
            "provider": provider or None,
            "status": "up" if success else "down",
            "last_check_at": datetime.now(UTC).isoformat(),
            "reason": None if success else reason,
        })
        await self._backend.kv_set_with_ttl(_key(address), value, self._ttl_seconds)

    async def get_many(self, addresses: list[str]) -> dict[str, IpHealthSnapshot]:
        """Return the current snapshot for each of *addresses*.

        Addresses with no recorded probe result (or whose entry has expired)
        map to an "unknown" snapshot rather than being omitted, so callers
        can render every requested address without a presence check.
        """
        wanted = set(addresses)
        rows = await self._backend.kv_list(_PREFIX)
        found: dict[str, IpHealthSnapshot] = {}
        for key, value in rows:
            address = key[len(_PREFIX):]
            if address not in wanted:
                continue
            try:
                data = json.loads(value)
            except (TypeError, ValueError):
                continue
            found[address] = IpHealthSnapshot(
                address=address,
                provider=data.get("provider"),
                status=data.get("status"),
                last_check_at=data.get("last_check_at"),
                reason=data.get("reason"),
            )
        return {address: found.get(address, _unknown(address)) for address in addresses}
