"""Tests for the `validate` CLI command's directory-source support.

Directory validation deliberately reuses FileConfigSource + a throwaway
ProxyRepository.reconcile() rather than a parallel implementation -- see
_validate_directory()'s docstring in cli.py. Single-file mode is untouched
(still load_config directly) and isn't re-tested here beyond one smoke case.
"""

from __future__ import annotations

import asyncio

import pytest
from click.testing import CliRunner

from proxy_hopper.cli import main


@pytest.fixture(autouse=True)
def _restore_event_loop_after_asyncio_run():
    """_validate_directory() calls asyncio.run(), which -- per its own
    documented behavior -- clears the process' "current" event loop when it
    finishes (not just closes the loop it made). Since these tests invoke
    the CLI command synchronously (via CliRunner, not pytest-asyncio's own
    loop management), that clears the loop for the rest of the pytest
    session too, breaking unrelated sync tests elsewhere that still rely on
    the deprecated asyncio.get_event_loop() auto-create fallback. Restore a
    usable loop afterward so this file doesn't leak that side effect."""
    yield
    asyncio.set_event_loop(asyncio.new_event_loop())


def _write(path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


class TestValidateDirectory:
    def test_valid_multi_file_directory_exits_zero(self, tmp_path):
        _write(tmp_path / "providers.yaml", (
            "proxyProviders:\n"
            "  - name: prov\n"
            "    ipList:\n"
            '      - "1.1.1.1:8080"\n'
        ))
        _write(tmp_path / "pools.yaml", (
            "ipPools:\n"
            "  - name: pool\n"
            "    ipRequests:\n"
            "      - provider: prov\n"
            "        count: 1\n"
        ))
        _write(tmp_path / "targets.yaml", (
            "targets:\n"
            "  - name: t\n"
            "    regex: '.*'\n"
            "    ipPool: pool\n"
        ))

        result = CliRunner().invoke(main, ["validate", "--config", str(tmp_path)])

        assert result.exit_code == 0, result.output
        assert "Providers: 1 defined." in result.output
        assert "'prov': 1 IP(s)" in result.output
        assert "Pools: 1 defined." in result.output
        assert "Config OK — 1 target(s) defined." in result.output
        assert "'t': 1 IP(s), pool='pool'" in result.output

    def test_unresolvable_target_reports_error_and_exits_nonzero(self, tmp_path):
        _write(tmp_path / "providers.yaml", (
            "proxyProviders:\n"
            "  - name: prov\n"
            "    ipList:\n"
            '      - "1.1.1.1:8080"\n'
        ))
        _write(tmp_path / "targets.yaml", (
            "targets:\n"
            "  - name: bad\n"
            "    regex: '.*'\n"
            "    ipPool: no-such-pool\n"
        ))

        result = CliRunner().invoke(main, ["validate", "--config", str(tmp_path)])

        assert result.exit_code == 1
        assert "bad" in result.output
        assert "no-such-pool" in result.output
        assert "targets.yaml" in result.output
        # The provider still validated fine -- one bad target doesn't hide
        # the rest of the report.
        assert "Providers: 1 defined." in result.output

    def test_duplicate_name_across_files_reports_warning_and_still_exits_zero(
        self, tmp_path
    ):
        _write(tmp_path / "01-first.yaml", (
            "targets:\n"
            "  - name: dup\n"
            "    regex: 'first'\n"
            "    ipPool: pool\n"
        ))
        _write(tmp_path / "02-second.yaml", (
            "targets:\n"
            "  - name: dup\n"
            "    regex: 'second'\n"
            "    ipPool: pool\n"
        ))
        _write(tmp_path / "pools.yaml", (
            "proxyProviders:\n"
            "  - name: prov\n"
            "    ipList:\n"
            '      - "1.1.1.1:8080"\n'
            "ipPools:\n"
            "  - name: pool\n"
            "    ipRequests:\n"
            "      - provider: prov\n"
            "        count: 1\n"
        ))

        result = CliRunner().invoke(main, ["validate", "--config", str(tmp_path)])

        assert result.exit_code == 0, result.output
        assert "Warning:" in result.output
        assert "01-first.yaml" in result.output
        assert "02-second.yaml" in result.output
        assert "Config OK — 1 target(s) defined." in result.output

    def test_malformed_file_reports_error_and_exits_nonzero(self, tmp_path):
        _write(tmp_path / "bad.yaml", "proxyProviders: [not, a, mapping")

        result = CliRunner().invoke(main, ["validate", "--config", str(tmp_path)])

        assert result.exit_code == 1
        assert "bad.yaml" in result.output

    def test_empty_directory_exits_zero(self, tmp_path):
        result = CliRunner().invoke(main, ["validate", "--config", str(tmp_path)])
        assert result.exit_code == 0, result.output
        assert "Config OK — 0 target(s) defined." in result.output


class TestValidateSingleFileUnchanged:
    def test_valid_single_file_still_works(self, tmp_path):
        p = tmp_path / "config.yaml"
        _write(p, (
            "proxyProviders:\n"
            "  - name: prov\n"
            "    ipList:\n"
            '      - "1.1.1.1:8080"\n'
            "ipPools:\n"
            "  - name: pool\n"
            "    ipRequests:\n"
            "      - provider: prov\n"
            "        count: 1\n"
            "targets:\n"
            "  - name: t\n"
            "    regex: '.*'\n"
            "    ipPool: pool\n"
        ))

        result = CliRunner().invoke(main, ["validate", "--config", str(p)])

        assert result.exit_code == 0, result.output
        assert "Config OK — 1 target(s) defined." in result.output
        assert "Server defaults:" in result.output

    def test_invalid_single_file_still_exits_nonzero(self, tmp_path):
        p = tmp_path / "config.yaml"
        _write(p, "targets:\n  - name: t\n    regex: '.*'\n    ipPool: missing\n")

        result = CliRunner().invoke(main, ["validate", "--config", str(p)])

        assert result.exit_code == 1
        assert "Config error" in result.output
