"""Tests for config loading and validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from proxy_hopper.config import (
    TargetConfig,
    _parse_duration,
    load_config,
)


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
        t = TargetConfig(name="foo", regex=r".*foo\.com.*", ip_list=["1.2.3.4:8080"])
        assert t.name == "foo"

    def test_invalid_regex_raises(self):
        with pytest.raises(ValidationError, match="Invalid regex"):
            TargetConfig(name="bad", regex="[invalid", ip_list=["1.2.3.4"])

    def test_empty_ip_list_raises(self):
        with pytest.raises(ValidationError):
            TargetConfig(name="foo", regex=".*", ip_list=[])

    def test_resolved_ip_list_with_port(self):
        t = TargetConfig(name="foo", regex=".*", ip_list=["10.0.0.1:3128"])
        assert t.resolved_ip_list() == [("10.0.0.1", 3128)]

    def test_resolved_ip_list_default_port(self):
        t = TargetConfig(name="foo", regex=".*", ip_list=["10.0.0.1"], default_proxy_port=8888)
        assert t.resolved_ip_list() == [("10.0.0.1", 8888)]

    def test_compiled_regex(self):
        t = TargetConfig(name="foo", regex=r".*google\.com.*", ip_list=["1.1.1.1"])
        pattern = t.compiled_regex()
        assert pattern.search("http://google.com/path")
        assert not pattern.search("http://bing.com/path")


class TestLoadConfig:
    def test_loads_targets(self, sample_yaml):
        targets = load_config(sample_yaml)
        assert len(targets) == 1
        t = targets[0]
        assert t.name == "example"
        assert t.min_request_interval == 1.0
        assert t.max_queue_wait == 10.0
        assert t.quarantine_time == 60.0

    def test_camel_case_ip_list(self, sample_yaml):
        targets = load_config(sample_yaml)
        resolved = targets[0].resolved_ip_list()
        assert ("10.0.0.1", 3128) in resolved
        assert ("10.0.0.2", 8080) in resolved  # default port

    def test_no_server_block_in_output(self, sample_yaml):
        targets = load_config(sample_yaml)
        # load_config returns a list, not a server config object
        assert isinstance(targets, list)

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_config(tmp_path / "nonexistent.yaml")
