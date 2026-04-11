"""Tests for config loading and validation."""

from __future__ import annotations

import os
from textwrap import dedent

import pytest
from pydantic import ValidationError

from proxy_hopper.config import (
    ProxyHopperConfig,
    ResolvedIP,
    ServerConfig,
    TargetConfig,
    _parse_duration,
    load_config,
)

from test_helpers import make_target_config


class TestParseDuration:
    def test_seconds(self):
        assert _parse_duration("30s") == 30.0

    def test_minutes(self):
        assert _parse_duration("2m") == 120.0

    def test_hours(self):
        assert _parse_duration("1h") == 3600.0

    def test_plain_string(self):
        assert _parse_duration("45") == 45.0

    def test_float(self):
        assert _parse_duration(1.5) == 1.5

    def test_int(self):
        assert _parse_duration(10) == 10.0


class TestTargetConfig:
    def test_valid_config(self):
        t = make_target_config(["1.2.3.4:8080"], name="foo", regex=r".*foo\.com.*")
        assert t.name == "foo"

    def test_invalid_regex_raises(self):
        with pytest.raises(ValidationError, match="Invalid regex"):
            make_target_config(["1.2.3.4:8080"], name="bad", regex="[invalid")

    def test_empty_ip_list_raises(self):
        with pytest.raises(ValidationError):
            TargetConfig(name="foo", regex=".*", resolved_ips=[])

    def test_resolved_ip_list_with_port(self):
        t = make_target_config(["10.0.0.1:3128"], name="foo", regex=".*")
        assert t.resolved_ip_list() == [("10.0.0.1", 3128)]

    def test_resolved_ip_list_default_port(self):
        t = TargetConfig(
            name="foo", regex=".*",
            resolved_ips=[ResolvedIP(host="10.0.0.1", port=8888)],
        )
        assert t.resolved_ip_list() == [("10.0.0.1", 8888)]

    def test_compiled_regex(self):
        t = make_target_config(["1.1.1.1:8080"], name="foo", regex=r".*google\.com.*")
        pattern = t.compiled_regex()
        assert pattern.search("http://google.com/path")
        assert not pattern.search("http://bing.com/path")


class TestLoadConfig:
    def test_loads_targets(self, sample_yaml):
        cfg = load_config(sample_yaml)
        assert isinstance(cfg, ProxyHopperConfig)
        assert len(cfg.targets) == 1
        t = cfg.targets[0]
        assert t.name == "example"
        assert t.min_request_interval == 1.0
        assert t.max_queue_wait == 10.0
        assert t.quarantine_time == 60.0

    def test_camel_case_ip_list(self, sample_yaml):
        cfg = load_config(sample_yaml)
        resolved = cfg.targets[0].resolved_ip_list()
        assert ("10.0.0.1", 3128) in resolved
        assert ("10.0.0.2", 8080) in resolved  # default port

    def test_returns_proxy_hopper_config(self, sample_yaml):
        cfg = load_config(sample_yaml)
        assert isinstance(cfg, ProxyHopperConfig)
        assert isinstance(cfg.server, ServerConfig)
        assert isinstance(cfg.targets, list)

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_config(tmp_path / "nonexistent.yaml")


class TestIPPools:
    def _write(self, tmp_path, content: str):
        p = tmp_path / "config.yaml"
        p.write_text(dedent(content))
        return p

    def test_target_referencing_pool_gets_pool_ips(self, tmp_path):
        p = self._write(tmp_path, """
            ipPools:
              - name: shared
                ipList:
                  - "1.1.1.1:3128"
                  - "2.2.2.2:3128"
            targets:
              - name: general
                regex: '.*'
                ipPool: shared
        """)
        cfg = load_config(p)
        assert cfg.targets[0].ip_list() == ["1.1.1.1:3128", "2.2.2.2:3128"]

    def test_target_with_inline_ip_list_unaffected(self, tmp_path):
        p = self._write(tmp_path, """
            ipPools:
              - name: shared
                ipList:
                  - "1.1.1.1:3128"
            targets:
              - name: direct
                regex: '.*'
                ipList:
                  - "9.9.9.9:3128"
        """)
        cfg = load_config(p)
        assert cfg.targets[0].ip_list() == ["9.9.9.9:3128"]

    def test_pool_and_inline_can_coexist_across_targets(self, tmp_path):
        p = self._write(tmp_path, """
            ipPools:
              - name: pool-a
                ipList:
                  - "1.1.1.1:3128"
            targets:
              - name: via-pool
                regex: '.*'
                ipPool: pool-a
              - name: inline
                regex: 'api\\.example\\.com'
                ipList:
                  - "2.2.2.2:3128"
        """)
        cfg = load_config(p)
        assert cfg.targets[0].ip_list() == ["1.1.1.1:3128"]
        assert cfg.targets[1].ip_list() == ["2.2.2.2:3128"]

    def test_multiple_targets_share_same_pool(self, tmp_path):
        p = self._write(tmp_path, """
            ipPools:
              - name: shared
                ipList:
                  - "1.1.1.1:3128"
                  - "2.2.2.2:3128"
            targets:
              - name: t1
                regex: 'a\\.com'
                ipPool: shared
              - name: t2
                regex: 'b\\.com'
                ipPool: shared
        """)
        cfg = load_config(p)
        assert cfg.targets[0].ip_list() == cfg.targets[1].ip_list()

    def test_unknown_pool_reference_raises(self, tmp_path):
        p = self._write(tmp_path, """
            targets:
              - name: broken
                regex: '.*'
                ipPool: nonexistent
        """)
        with pytest.raises(ValueError, match="unknown ipPool 'nonexistent'"):
            load_config(p)

    def test_both_ip_pool_and_ip_list_raises(self, tmp_path):
        p = self._write(tmp_path, """
            ipPools:
              - name: pool-a
                ipList:
                  - "1.1.1.1:3128"
            targets:
              - name: conflict
                regex: '.*'
                ipPool: pool-a
                ipList:
                  - "2.2.2.2:3128"
        """)
        with pytest.raises(ValueError, match="both ipPool and ipList"):
            load_config(p)

    def test_pool_camel_case_ip_list(self, tmp_path):
        """ipList in pool block is normalised from camelCase."""
        p = self._write(tmp_path, """
            ipPools:
              - name: p
                ipList:
                  - "1.1.1.1:3128"
            targets:
              - name: t
                regex: '.*'
                ipPool: p
        """)
        cfg = load_config(p)
        assert "1.1.1.1:3128" in cfg.targets[0].ip_list()


class TestServerConfig:
    def _write(self, tmp_path, content: str):
        p = tmp_path / "config.yaml"
        p.write_text(dedent(content))
        return p

    def test_defaults_when_no_server_block(self, tmp_path):
        p = self._write(tmp_path, """
            targets:
              - name: t
                regex: '.*'
                ipList: ["1.1.1.1:3128"]
        """)
        cfg = load_config(p)
        assert cfg.server.host == "0.0.0.0"
        assert cfg.server.port == 8080
        assert cfg.server.backend == "memory"
        assert cfg.server.metrics is False

    def test_yaml_server_block_applied(self, tmp_path):
        p = self._write(tmp_path, """
            server:
              port: 9000
              backend: redis
              logLevel: DEBUG
            targets:
              - name: t
                regex: '.*'
                ipList: ["1.1.1.1:3128"]
        """)
        cfg = load_config(p)
        assert cfg.server.port == 9000
        assert cfg.server.backend == "redis"
        assert cfg.server.log_level == "DEBUG"

    def test_env_var_applied_when_no_yaml_server_block(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PROXY_HOPPER_PORT", "7777")
        monkeypatch.setenv("PROXY_HOPPER_BACKEND", "redis")
        p = self._write(tmp_path, """
            targets:
              - name: t
                regex: '.*'
                ipList: ["1.1.1.1:3128"]
        """)
        cfg = load_config(p)
        assert cfg.server.port == 7777
        assert cfg.server.backend == "redis"

    def test_yaml_overrides_env_var(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PROXY_HOPPER_PORT", "5555")
        p = self._write(tmp_path, """
            server:
              port: 9000
            targets:
              - name: t
                regex: '.*'
                ipList: ["1.1.1.1:3128"]
        """)
        cfg = load_config(p)
        # YAML (9000) beats env var (5555)
        assert cfg.server.port == 9000

    def test_probe_urls_as_list_in_yaml(self, tmp_path):
        p = self._write(tmp_path, """
            server:
              probe: true
              probeUrls:
                - https://1.1.1.1
                - https://8.8.8.8
            targets:
              - name: t
                regex: '.*'
                ipList: ["1.1.1.1:3128"]
        """)
        cfg = load_config(p)
        assert cfg.server.probe is True
        assert cfg.server.probe_urls == ["https://1.1.1.1", "https://8.8.8.8"]

    def test_probe_urls_from_env_var_comma_separated(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PROXY_HOPPER_PROBE_URLS", "https://a.example,https://b.example")
        p = self._write(tmp_path, """
            targets:
              - name: t
                regex: '.*'
                ipList: ["1.1.1.1:3128"]
        """)
        cfg = load_config(p)
        assert cfg.server.probe_urls == ["https://a.example", "https://b.example"]

    def test_metrics_bool_env_var_truthy_values(self, tmp_path, monkeypatch):
        for val in ("true", "True", "1", "yes", "on"):
            monkeypatch.setenv("PROXY_HOPPER_METRICS", val)
            p = self._write(tmp_path, """
                targets:
                  - name: t
                    regex: '.*'
                    ipList: ["1.1.1.1:3128"]
            """)
            cfg = load_config(p)
            assert cfg.server.metrics is True, f"Expected True for env val={val!r}"
