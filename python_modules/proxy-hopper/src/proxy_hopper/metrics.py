"""Optional Prometheus metrics for Proxy Hopper.

Enabled by setting PROXY_HOPPER_METRICS=true (or passing --metrics to the CLI).
The /metrics endpoint is served by prometheus_client's built-in HTTP server
on PROXY_HOPPER_METRICS_PORT (default 9090).  No web framework is required.

Metrics exposed:
  proxy_hopper_requests_total{target, outcome}         Counter
  proxy_hopper_request_duration_seconds{target}        Histogram
  proxy_hopper_queue_depth{target}                     Gauge
  proxy_hopper_available_ips{target}                   Gauge
  proxy_hopper_quarantined_ips{target}                 Gauge

  proxy_hopper_probe_success_total{address}            Counter
  proxy_hopper_probe_failure_total{address, reason}    Counter
  proxy_hopper_probe_duration_seconds{address}         Histogram
  proxy_hopper_ip_reachable{address}                   Gauge  (1=up, 0=down)
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING

logger = logging.getLogger(__name__)

try:
    from prometheus_client import (
        Counter,
        Gauge,
        Histogram,
        start_http_server,
    )
    _PROMETHEUS_AVAILABLE = True
except ImportError:  # pragma: no cover
    _PROMETHEUS_AVAILABLE = False

if TYPE_CHECKING:
    pass


class _NoopMetrics:
    """Null-object metrics collector used when prometheus is disabled or unavailable."""
    def record_request(self, target: str, outcome: str, duration: float) -> None:
        pass
    def set_queue_depth(self, target: str, depth: int) -> None:
        pass
    def set_available_ips(self, target: str, count: int) -> None:
        pass
    def set_quarantined_ips(self, target: str, count: int) -> None:
        pass
    def record_probe_success(self, address: str, duration: float) -> None:
        pass
    def record_probe_failure(self, address: str, reason: str, duration: float) -> None:
        pass


class PrometheusMetrics:
    """Thin wrapper around prometheus_client metrics."""

    def __init__(self) -> None:
        self._requests = Counter(
            "proxy_hopper_requests_total",
            "Total number of proxied requests",
            ["target", "outcome"],
        )
        self._duration = Histogram(
            "proxy_hopper_request_duration_seconds",
            "Duration of outbound proxy requests",
            ["target"],
            buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
        )
        self._queue_depth = Gauge(
            "proxy_hopper_queue_depth",
            "Number of requests waiting for an IP",
            ["target"],
        )
        self._available_ips = Gauge(
            "proxy_hopper_available_ips",
            "Number of IPs currently available in the pool",
            ["target"],
        )
        self._quarantined_ips = Gauge(
            "proxy_hopper_quarantined_ips",
            "Number of IPs currently quarantined",
            ["target"],
        )
        self._probe_success = Counter(
            "proxy_hopper_probe_success_total",
            "Total number of successful background IP probes",
            ["address"],
        )
        self._probe_failure = Counter(
            "proxy_hopper_probe_failure_total",
            "Total number of failed background IP probes",
            ["address", "reason"],
        )
        self._probe_duration = Histogram(
            "proxy_hopper_probe_duration_seconds",
            "Duration of background IP probe requests",
            ["address"],
            buckets=(0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
        )
        self._ip_reachable = Gauge(
            "proxy_hopper_ip_reachable",
            "Whether the proxy IP is reachable (1=up, 0=down)",
            ["address"],
        )

    def record_request(self, target: str, outcome: str, duration: float) -> None:
        self._requests.labels(target=target, outcome=outcome).inc()
        self._duration.labels(target=target).observe(duration)

    def set_queue_depth(self, target: str, depth: int) -> None:
        self._queue_depth.labels(target=target).set(depth)

    def set_available_ips(self, target: str, count: int) -> None:
        self._available_ips.labels(target=target).set(count)

    def set_quarantined_ips(self, target: str, count: int) -> None:
        self._quarantined_ips.labels(target=target).set(count)

    def record_probe_success(self, address: str, duration: float) -> None:
        self._probe_success.labels(address=address).inc()
        self._probe_duration.labels(address=address).observe(duration)
        self._ip_reachable.labels(address=address).set(1)

    def record_probe_failure(self, address: str, reason: str, duration: float) -> None:
        self._probe_failure.labels(address=address, reason=reason).inc()
        self._probe_duration.labels(address=address).observe(duration)
        self._ip_reachable.labels(address=address).set(0)


# Singleton — created once at startup
_collector: _NoopMetrics | PrometheusMetrics = _NoopMetrics()


def get_metrics() -> _NoopMetrics | PrometheusMetrics:
    return _collector


def start_metrics_server(port: int) -> None:
    """Start the Prometheus /metrics HTTP server on the given port.

    This is a blocking call that starts a daemon thread in the background.
    Safe to call once from the main asyncio thread at startup.
    """
    global _collector

    if not _PROMETHEUS_AVAILABLE:  # pragma: no cover
        logger.warning(
            "prometheus-client is not installed — metrics disabled. "
            "Install proxy-hopper[metrics] to enable."
        )
        return

    _collector = PrometheusMetrics()
    start_http_server(port)
    logger.info("Prometheus metrics available on :%d/metrics", port)
