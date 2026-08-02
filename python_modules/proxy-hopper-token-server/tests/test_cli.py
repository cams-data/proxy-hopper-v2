"""Smoke tests for the `ph-token-server` CLI wiring.

These don't start a real server (TokenServer.run() blocks forever) — they
patch the resolved server's .run() method and assert the CLI resolves the
import path and applies CLI flags (host/port/timeout) to the instance before
calling it.
"""

from __future__ import annotations

import sys
import types
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from proxy_hopper_token_server import TokenProvider, TokenRequest, TokenResponse, TokenServer
from proxy_hopper_token_server.cli import main


class _DummyProvider(TokenProvider):
    async def get_token(self, req: TokenRequest) -> TokenResponse:
        return TokenResponse(headers={}, expires_at=datetime.now(UTC) + timedelta(hours=1), cursor={})


def _make_module(name: str, **attrs) -> types.ModuleType:
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    return mod


def test_start_resolves_provider_and_applies_cli_flags():
    server = TokenServer(provider=_DummyProvider(), host="0.0.0.0", port=9000, timeout=30.0)
    server.run = MagicMock()
    runner = CliRunner()

    with patch("proxy_hopper_token_server._internal.loader.resolve", return_value=server):
        result = runner.invoke(
            main,
            ["start", "ignored:path", "--host", "127.0.0.1", "--port", "9123", "--timeout", "5"],
        )

    assert result.exit_code == 0, result.output
    assert "Serving on http://127.0.0.1:9123" in result.output
    server.run.assert_called_once()
    # CLI flags must override the instance's defaults before run() is called.
    assert server.host == "127.0.0.1"
    assert server.port == 9123
    assert server.timeout == 5.0


def test_start_end_to_end_resolves_real_provider_module():
    """No mocking of the loader — resolves a real module:attr path, only
    TokenServer.run() itself is patched so the test doesn't block forever."""
    _make_module("_test_cli_tokens", provider=_DummyProvider())
    runner = CliRunner()

    with patch("proxy_hopper_token_server.server.TokenServer.run") as mock_run:
        result = runner.invoke(main, ["start", "_test_cli_tokens:provider"])

    assert result.exit_code == 0, result.output
    mock_run.assert_called_once()


def test_start_reports_bad_import_path():
    runner = CliRunner()
    result = runner.invoke(main, ["start", "not_a_valid_path"])
    assert result.exit_code != 0
    assert "Invalid import path" in result.output


def test_start_reports_missing_module():
    runner = CliRunner()
    result = runner.invoke(main, ["start", "definitely.not.a.real.module:attr"])
    assert result.exit_code != 0
    assert "Cannot import module" in result.output
