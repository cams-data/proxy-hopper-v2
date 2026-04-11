"""Shared test helpers for constructing test fixtures."""

from __future__ import annotations

from proxy_hopper.config import ResolvedIP, TargetConfig


def make_target_config(ip_list: list[str], **kwargs) -> TargetConfig:
    """Build a TargetConfig from a plain list of 'host:port' strings."""
    resolved_ips = []
    for entry in ip_list:
        host, _, port_str = entry.rpartition(":")
        resolved_ips.append(ResolvedIP(host=host, port=int(port_str)))
    defaults = dict(
        name="test-target",
        regex=r".*example\.com.*",
        min_request_interval=0.0,
        max_queue_wait=5.0,
        num_retries=2,
        ip_failures_until_quarantine=3,
        quarantine_time=0.5,
    )
    defaults.update(kwargs)
    return TargetConfig(resolved_ips=resolved_ips, **defaults)
