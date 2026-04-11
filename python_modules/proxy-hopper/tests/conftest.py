"""Fixtures for proxy-hopper package tests."""

from __future__ import annotations

import sys
from pathlib import Path
from textwrap import dedent

import pytest
import pytest_asyncio

# Make the tests/ directory importable so test_helpers can be imported
# by test modules directly (e.g. `from test_helpers import make_target_config`).
sys.path.insert(0, str(Path(__file__).parent))

from proxy_hopper.backend.memory import MemoryIPPoolBackend
from proxy_hopper.config import TargetConfig, load_config
from test_helpers import make_target_config


@pytest.fixture
def target_config() -> TargetConfig:
    return make_target_config(["1.2.3.4:8080", "5.6.7.8:8080"])


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
