"""Server-side Prometheus query client for the admin API's per-target metrics panel.

Used instead of proxy_hopper.app_metrics's in-process counters when
``server.prometheusUrl`` is configured. Queries run here, server-side, and
only the already-aggregated result crosses into the GraphQL response — the
browser never talks to Prometheus directly. That keeps the existing admin
auth/RBAC boundary as the only way in, and doesn't require exposing
Prometheus's own HTTP API to the network the admin UI runs on.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, datetime

import httpx

from proxy_hopper.app_metrics import TargetMetricsSnapshot
from proxy_hopper.ip_health import IpHealthSnapshot

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 5.0


async def _instant_query(client: httpx.AsyncClient, base_url: str, promql: str) -> float:
    """Run one Prometheus instant query, returning 0.0 on any failure.

    A single missing/erroring series must not blank the whole panel — the
    admin UI would rather show a partial, slightly-wrong number than an
    error for something this low-stakes.
    """
    try:
        resp = await client.get(f"{base_url}/api/v1/query", params={"query": promql})
        resp.raise_for_status()
        data = resp.json()
        result = data.get("data", {}).get("result", [])
        if not result:
            return 0.0
        return float(result[0]["value"][1])
    except (httpx.HTTPError, KeyError, ValueError, IndexError, TypeError) as exc:
        logger.warning("Prometheus query failed (%r): %s", promql, exc)
        return 0.0


async def _instant_query_vector(
    client: httpx.AsyncClient, base_url: str, promql: str,
) -> list[dict]:
    """Run one Prometheus instant query, returning the raw result vector
    (a list of ``{"metric": {...labels}, "value": [ts, val]}`` rows) instead
    of a single collapsed float — for queries that return one row per label
    set, like ``query_ip_health`` below. Empty list on any failure, same
    "partial over error" rationale as ``_instant_query``.
    """
    try:
        resp = await client.get(f"{base_url}/api/v1/query", params={"query": promql})
        resp.raise_for_status()
        data = resp.json()
        return data.get("data", {}).get("result", [])
    except (httpx.HTTPError, KeyError, ValueError, TypeError) as exc:
        logger.warning("Prometheus query failed (%r): %s", promql, exc)
        return []


async def query_target_metrics(prometheus_url: str, target: str) -> TargetMetricsSnapshot:
    """Query Prometheus for aggregate request stats for *target*.

    Reads the same ``proxy_hopper_requests_total`` /
    ``proxy_hopper_request_duration_seconds`` series the core proxy exposes
    on its own ``/metrics`` endpoint (see ``metrics.py``) — this only works
    if that endpoint is enabled and something is actually scraping it into
    *prometheus_url*.

    ``last_request_at`` is always ``None``: Prometheus's counters carry no
    "last event" timestamp without a dedicated gauge, which this doesn't add
    — that field is only ever populated by the in-process counters tier.
    """
    base_url = prometheus_url.rstrip("/")
    target_selector = target.replace('"', '\\"')

    total_q = f'sum(proxy_hopper_requests_total{{target="{target_selector}"}})'
    success_q = f'sum(proxy_hopper_requests_total{{target="{target_selector}", outcome="success"}})'
    latency_sum_q = f'sum(proxy_hopper_request_duration_seconds_sum{{target="{target_selector}"}})'
    latency_count_q = f'sum(proxy_hopper_request_duration_seconds_count{{target="{target_selector}"}})'

    async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
        total, success, latency_sum, latency_count = await asyncio.gather(
            _instant_query(client, base_url, total_q),
            _instant_query(client, base_url, success_q),
            _instant_query(client, base_url, latency_sum_q),
            _instant_query(client, base_url, latency_count_q),
        )

    total_i = int(total)
    success_i = min(int(success), total_i)
    failed_i = max(0, total_i - success_i)
    avg_latency_ms = (latency_sum / latency_count * 1000) if latency_count else 0.0

    return TargetMetricsSnapshot(
        name=target,
        total_requests=total_i,
        success_requests=success_i,
        failed_requests=failed_i,
        avg_latency_ms=avg_latency_ms,
        last_request_at=None,
    )


async def query_ip_health(prometheus_url: str, addresses: list[str]) -> dict[str, IpHealthSnapshot]:
    """Query Prometheus for the current reachability of each of *addresses*.

    Reads ``proxy_hopper_ip_reachable`` (see ``prober.py``'s docstring) — one
    query covering every address via a label regex, rather than one query
    per address, since a pool/provider can have many IPs.

    ``reason`` is always ``None``: the gauge carries no failure-reason label
    (only the ``proxy_hopper_probe_failure_total`` counter does), which this
    doesn't cross-reference — same "field this source genuinely can't answer
    stays None" precedent as ``query_target_metrics``'s ``last_request_at``.
    Addresses with no matching series come back as "unknown" (``status=None``),
    same as ``IpHealthStore.get_many``'s never-probed case.
    """
    if not addresses:
        return {}
    base_url = prometheus_url.rstrip("/")
    pattern = "|".join(re.escape(addr) for addr in addresses)
    query = f'proxy_hopper_ip_reachable{{address=~"{pattern}"}}'

    async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
        rows = await _instant_query_vector(client, base_url, query)

    found: dict[str, IpHealthSnapshot] = {}
    for row in rows:
        try:
            metric = row["metric"]
            address = metric["address"]
            value = float(row["value"][1])
            checked_at = datetime.fromtimestamp(float(row["value"][0]), tz=UTC).isoformat()
        except (KeyError, ValueError, TypeError, IndexError):
            continue
        found[address] = IpHealthSnapshot(
            address=address,
            provider=metric.get("provider") or None,
            status="up" if value else "down",
            last_check_at=checked_at,
            reason=None,
        )

    return {
        address: found.get(address) or IpHealthSnapshot(
            address=address, provider=None, status=None, last_check_at=None, reason=None,
        )
        for address in addresses
    }
