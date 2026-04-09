"""Fixtures for proxy-hopper package tests."""

from __future__ import annotations

from textwrap import dedent

import pytest
import pytest_asyncio

from proxy_hopper.backend.memory import MemoryIPPoolBackend
from proxy_hopper.config import TargetConfig, load_config


@pytest.fixture
def target_config() -> TargetConfig:
    return TargetConfig(
        name="test-target",
        regex=r".*example\.com.*",
        ip_list=["1.2.3.4:8080", "5.6.7.8:8080"],
        min_request_interval=0.0,
        max_queue_wait=5.0,
        num_retries=2,
        ip_failures_until_quarantine=3,
        quarantine_time=0.5,
    )


@pytest_asyncio.fixture
async def memory_backend(target_config) -> MemoryIPPoolBackend:
    backend = MemoryIPPoolBackend()
    await backend.start()
    await backend.init_target(target_config.name)
    for host, port in target_config.resolved_ip_list():
        await backend.push_ip(target_config.name, f"{host}:{port}")
    yield backend
    await backend.stop()


@pytest.fixture
def sample_yaml(tmp_path) -> str:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(dedent(r"""
        targets:
          - name: example
            regex: '.*example\.com.*'
            ipList:
              - "10.0.0.1:3128"
              - "10.0.0.2"
            minRequestInterval: 1s
            maxQueueWait: 10s
            numRetries: 3
            ipFailuresUntilQuarantine: 5
            quarantineTime: 60s
            defaultProxyPort: 8080
    """))
    return str(cfg)
