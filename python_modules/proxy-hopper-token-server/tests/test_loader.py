"""Tests for the import-path loader."""

from __future__ import annotations

import sys
import types

import pytest

from proxy_hopper_token_server._internal.loader import resolve
from proxy_hopper_token_server import TokenProvider, TokenRequest, TokenResponse, TokenServer
from datetime import UTC, datetime, timedelta


class _DummyProvider(TokenProvider):
    async def get_token(self, req: TokenRequest) -> TokenResponse:
        return TokenResponse(headers={}, expires_at=datetime.now(UTC) + timedelta(hours=1), cursor={})


def _make_module(name: str, **attrs) -> types.ModuleType:
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    return mod


def test_resolve_provider_instance():
    _make_module("_test_tokens_inst", provider=_DummyProvider())
    server = resolve("_test_tokens_inst:provider")
    assert isinstance(server, TokenServer)


def test_resolve_provider_class():
    _make_module("_test_tokens_cls", MyProvider=_DummyProvider)
    server = resolve("_test_tokens_cls:MyProvider")
    assert isinstance(server, TokenServer)


def test_resolve_server_instance():
    existing = TokenServer(provider=_DummyProvider(), port=9999)
    _make_module("_test_tokens_srv", server=existing)
    result = resolve("_test_tokens_srv:server")
    assert result is existing


def test_resolve_bad_format():
    import click
    with pytest.raises(click.ClickException):
        resolve("no_colon_here")


def test_resolve_missing_module():
    import click
    with pytest.raises(click.ClickException):
        resolve("definitely.not.a.real.module:attr")


def test_resolve_wrong_type():
    _make_module("_test_tokens_wrong", value=42)
    import click
    with pytest.raises(click.ClickException):
        resolve("_test_tokens_wrong:value")
