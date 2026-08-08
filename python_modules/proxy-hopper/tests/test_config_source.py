"""Tests for FileConfigSource -- see CONFIG_RECONCILER_SCOPE.md §4/Phase 3."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from proxy_hopper.config import load_config
from proxy_hopper.config_source import scan_config_source


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dedent(content))


class TestSingleFileBackCompat:
    def test_matches_load_config_providers_and_pools(self, sample_yaml):
        legacy = load_config(sample_yaml)
        merged, warnings, errors = scan_config_source(sample_yaml)

        assert warnings == []
        assert errors == []
        assert [p.provider.name for p in merged.providers] == [p.name for p in legacy.providers]
        assert [p.pool.name for p in merged.pools] == [p.name for p in legacy.pools]

    def test_target_spec_matches_legacy_target_fields(self, sample_yaml):
        legacy = load_config(sample_yaml)
        merged, _, _ = scan_config_source(sample_yaml)

        assert len(merged.target_specs) == 1
        spec = merged.target_specs[0]
        legacy_target = legacy.targets[0]
        assert spec.fields["name"] == legacy_target.name
        assert spec.fields["regex"] == legacy_target.regex
        assert spec.pool_ref == legacy_target.pool_name
        assert spec.fields["min_request_interval"] == legacy_target.min_request_interval
        assert spec.fields["max_queue_wait"] == legacy_target.max_queue_wait

    def test_source_file_is_the_single_file_name(self, sample_yaml):
        merged, _, _ = scan_config_source(sample_yaml)
        assert merged.providers[0].source_file == "config.yaml"
        assert merged.pools[0].source_file == "config.yaml"
        assert merged.target_specs[0].source_file == "config.yaml"


class TestMultipleFilesNoOverlap:
    def test_all_entities_present(self, tmp_path):
        _write(tmp_path / "providers.yaml", """
            proxyProviders:
              - name: prov-a
                ipList: ["1.1.1.1:8080"]
        """)
        _write(tmp_path / "pools.yaml", """
            ipPools:
              - name: pool-a
                ipRequests:
                  - provider: prov-a
                    count: 1
        """)
        _write(tmp_path / "targets.yaml", """
            targets:
              - name: target-a
                regex: '.*'
                ipPool: pool-a
        """)

        merged, warnings, errors = scan_config_source(tmp_path)

        assert warnings == []
        assert errors == []
        assert [p.provider.name for p in merged.providers] == ["prov-a"]
        assert [p.pool.name for p in merged.pools] == ["pool-a"]
        assert [t.fields["name"] for t in merged.target_specs] == ["target-a"]


class TestDuplicateNamePrecedence:
    def test_earlier_sorted_file_wins_target(self, tmp_path):
        _write(tmp_path / "01-first.yaml", """
            targets:
              - name: dup
                regex: 'first'
                ipPool: some-pool
        """)
        _write(tmp_path / "02-second.yaml", """
            targets:
              - name: dup
                regex: 'second'
                ipPool: some-pool
        """)

        merged, warnings, errors = scan_config_source(tmp_path)

        assert errors == []
        assert len(merged.target_specs) == 1
        assert merged.target_specs[0].fields["regex"] == "first"
        assert merged.target_specs[0].source_file == "01-first.yaml"
        assert len(warnings) == 1
        assert "dup" in warnings[0]
        assert "01-first.yaml" in warnings[0]
        assert "02-second.yaml" in warnings[0]

    def test_provider_duplicate_within_one_file_warns_not_raises(self, tmp_path):
        # Deliberate deviation from load_config's single-file behavior (which
        # raises ValueError here) -- see the module docstring.
        _write(tmp_path / "config.yaml", """
            proxyProviders:
              - name: dup-provider
                ipList: ["1.1.1.1:8080"]
              - name: dup-provider
                ipList: ["2.2.2.2:8080"]
        """)

        merged, warnings, errors = scan_config_source(tmp_path)

        assert errors == []
        assert len(merged.providers) == 1
        assert merged.providers[0].provider.ip_list == ["1.1.1.1:8080"]
        assert len(warnings) == 1
        assert "dup-provider" in warnings[0]


class TestNestedSubdirectoriesAndSortOrder:
    def test_surprising_sort_order_a_yaml_before_a_slash_b_yaml(self, tmp_path):
        # 'a.yaml' sorts before 'a/b.yaml' as plain strings ('.' < '/' is
        # false in ASCII -- '/' is 0x2F, '.' is 0x2E, so 'a.yaml' < 'a/b.yaml'
        # lexicographically because '.' < '/'). Lock this in explicitly.
        _write(tmp_path / "a.yaml", """
            targets:
              - name: shadow-target
                regex: 'top-level'
                ipPool: p
        """)
        _write(tmp_path / "a" / "b.yaml", """
            targets:
              - name: shadow-target
                regex: 'nested'
                ipPool: p
        """)

        merged, warnings, errors = scan_config_source(tmp_path)

        assert errors == []
        assert len(merged.target_specs) == 1
        assert merged.target_specs[0].fields["regex"] == "top-level"
        assert merged.target_specs[0].source_file == "a.yaml"
        assert len(warnings) == 1
        assert "a.yaml" in warnings[0]
        assert "a/b.yaml" in warnings[0]

    def test_deeply_nested_file_is_included(self, tmp_path):
        _write(tmp_path / "providers" / "region" / "aws.yaml", """
            proxyProviders:
              - name: nested-provider
                ipList: ["9.9.9.9:8080"]
        """)

        merged, warnings, errors = scan_config_source(tmp_path)

        assert errors == []
        assert warnings == []
        assert [p.provider.name for p in merged.providers] == ["nested-provider"]
        assert merged.providers[0].source_file == "providers/region/aws.yaml"


class TestParseFailures:
    def test_malformed_file_among_valid_ones_reported_as_error_not_warning(self, tmp_path):
        _write(tmp_path / "01-bad.yaml", """
            proxyProviders:
              - name: [this is not a valid provider
        """)
        _write(tmp_path / "02-good.yaml", """
            proxyProviders:
              - name: good-provider
                ipList: ["1.1.1.1:8080"]
        """)

        merged, warnings, errors = scan_config_source(tmp_path)

        assert warnings == []
        assert len(errors) == 1
        assert "01-bad.yaml" in errors[0]
        assert [p.provider.name for p in merged.providers] == ["good-provider"]

    def test_target_missing_pool_reference_is_an_error(self, tmp_path):
        _write(tmp_path / "config.yaml", """
            targets:
              - name: no-pool
                regex: '.*'
        """)

        merged, warnings, errors = scan_config_source(tmp_path)

        assert merged.target_specs == []
        assert len(errors) == 1
        assert "config.yaml" in errors[0]

    def test_all_files_malformed_result_is_empty_with_errors(self, tmp_path):
        _write(tmp_path / "01-bad.yaml", "proxyProviders: [not, a, mapping]")
        _write(tmp_path / "02-bad.yaml", "targets: {name: not-a-list}")

        merged, warnings, errors = scan_config_source(tmp_path)

        assert merged.is_empty
        assert len(errors) == 2

    def test_empty_directory_is_empty_with_no_errors(self, tmp_path):
        merged, warnings, errors = scan_config_source(tmp_path)

        assert merged.is_empty
        assert warnings == []
        assert errors == []

    def test_directory_with_no_yaml_files_is_empty_with_no_errors(self, tmp_path):
        (tmp_path / "readme.txt").write_text("not yaml")

        merged, warnings, errors = scan_config_source(tmp_path)

        assert merged.is_empty
        assert errors == []


class TestRootDoesNotExist:
    def test_raises_file_not_found(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            scan_config_source(tmp_path / "does-not-exist")
