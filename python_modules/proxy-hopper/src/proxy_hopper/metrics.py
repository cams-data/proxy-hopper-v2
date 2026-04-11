"""Optional Prometheus metrics for Proxy Hopper.

Enabled by setting PROXY_HOPPER_METRICS=true (or passing --metrics to the CLI).
The /metrics endpoint is served by prometheus_client's built-in HTTP server
on PROXY_HOPPER_METRICS_PORT (default 9090).  No web framework is required.

Metrics exposed
---------------
Request pipeline
  proxy_hopper_requests_total{target, outcome}              Counter
  proxy_hopper_request_duration_seconds{target}             Histogram
  proxy_hopper_responses_total{target, status_code}         Counter
  proxy_hopper_retries_total{target}                        Counter
  proxy_hopper_retry_exhaustions_total{target}              Counter

Queue
  proxy_hopper_queue_depth{target}                          Gauge
  proxy_hopper_queue_wait_seconds{target}                   Histogram
  proxy_hopper_queue_expired_total{target}                  Counter

Connections
  proxy_hopper_active_connections                           Gauge

IP pool
  proxy_hopper_available_ips{target}                        Gauge
  proxy_hopper_quarantined_ips{target}                      Gauge
  proxy_hopper_ip_quarantine_events_total{target, address}  Counter
  proxy_hopper_ip_failure_count{target, address}            Gauge

Probes
  proxy_hopper_probe_success_total{address}                 Counter
  proxy_hopper_probe_failure_total{address, reason}         Counter
  proxy_hopper_probe_duration_seconds{address}              Histogram
  proxy_hopper_ip_reachable{address}                        Gauge  (1=up, 0=down)
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
    def record_response(self, target: str, status_code: int) -> None:
        pass
    def record_retry(self, target: str) -> None:
        pass
    def record_retry_exhaustion(self, target: str) -> None:
        pass
    def set_queue_depth(self, target: str, depth: int) -> None:
        pass
    def record_queue_wait(self, target: str, seconds: float) -> None:
        pass
    def record_queue_expired(self, target: str) -> None:
        pass
    def inc_active_connections(self) -> None:
        pass
    def dec_active_connections(self) -> None:
        pass
    def set_available_ips(self, target: str, count: int) -> None:
        pass
    def set_quarantined_ips(self, target: str, count: int) -> None:
        pass
    def record_quarantine_event(self, target: str, address: str) -> None:
        pass
    def set_ip_failure_count(self, target: str, address: str, count: int) -> None:
        pass
    def record_probe_success(self, address: str, duration: float) -> None:
        pass
    def record_probe_failure(self, address: str, reason: str, duration: float) -> None:
        pass


class PrometheusMetrics:
    """Thin wrapper around prometheus_client metrics."""

    def __init__(self) -> None:
        # --- Request pipeline ---
        self._requests = Counter(
            "proxy_hopper_requests_total",
            "Total outbound proxy requests by target and outcome",
            ["target", "outcome"],
        )
        self._duration = Histogram(
            "proxy_hopper_request_duration_seconds",
            "End-to-end duration of outbound proxy requests (excludes queue wait)",
            ["target"],
            buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
        )
        self._responses = Counter(
            "proxy_hopper_responses_total",
            "Total responses returned to clients, by target and HTTP status code",
            ["target", "status_code"],
        )
        self._retries = Counter(
            "proxy_hopper_retries_total",
            "Total retry attempts (each re-queue counts as one)",
            ["target"],
        )
        self._retry_exhaustions = Counter(
            "proxy_hopper_retry_exhaustions_total",
            "Requests that exhausted all retries and returned an error to the client",
            ["target"],
        )

        # --- Queue ---
        self._queue_depth = Gauge(
            "proxy_hopper_queue_depth",
            "Number of requests currently waiting for an IP",
            ["target"],
        )
        self._queue_wait = Histogram(
            "proxy_hopper_queue_wait_seconds",
            "Time a request waited in the queue before being dispatched to an IP",
            ["target"],
            buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
        )
        self._queue_expired = Counter(
            "proxy_hopper_queue_expired_total",
            "Requests dropped because they exceeded max_queue_wait before dispatch",
            ["target"],
        )

        # --- Connections ---
        self._active_connections = Gauge(
            "proxy_hopper_active_connections",
            "Number of currently open client connections to proxy-hopper",
        )

        # --- IP pool ---
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
        self._quarantine_events = Counter(
            "proxy_hopper_ip_quarantine_events_total",
            "Total number of times an IP has been quarantined",
            ["target", "address"],
        )
        self._ip_failure_count = Gauge(
            "proxy_hopper_ip_failure_count",
            "Current consecutive failure count for a proxy IP (resets to 0 on success)",
            ["target", "address"],
        )

        # --- Probes ---
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

    def record_response(self, target: str, status_code: int) -> None:
        self._responses.labels(target=target, status_code=str(status_code)).inc()

    def record_retry(self, target: str) -> None:
        self._retries.labels(target=target).inc()

    def record_retry_exhaustion(self, target: str) -> None:
        self._retry_exhaustions.labels(target=target).inc()

    def set_queue_depth(self, target: str, depth: int) -> None:
        self._queue_depth.labels(target=target).set(depth)

    def record_queue_wait(self, target: str, seconds: float) -> None:
        self._queue_wait.labels(target=target).observe(seconds)

    def record_queue_expired(self, target: str) -> None:
        self._queue_expired.labels(target=target).inc()

    def inc_active_connections(self) -> None:
        self._active_connections.inc()

    def dec_active_connections(self) -> None:
        self._active_connections.dec()

    def set_available_ips(self, target: str, count: int) -> None:
        self._available_ips.labels(target=target).set(count)

    def set_quarantined_ips(self, target: str, count: int) -> None:
        self._quarantined_ips.labels(target=target).set(count)

    def record_quarantine_event(self, target: str, address: str) -> None:
        self._quarantine_events.labels(target=target, address=address).inc()

    def set_ip_failure_count(self, target: str, address: str, count: int) -> None:
        self._ip_failure_count.labels(target=target, address=address).set(count)

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
