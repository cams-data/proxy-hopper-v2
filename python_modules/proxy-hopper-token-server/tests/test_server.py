"""Tests for the token server HTTP layer."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from proxy_hopper_token_server import TokenProvider, TokenRequest, TokenResponse
from proxy_hopper_token_server._internal.app import create_app


class _FixedProvider(TokenProvider):
    def __init__(self, headers: dict, hours: int = 1):
        self._headers = headers
        self._hours = hours

    async def get_token(self, request: TokenRequest) -> TokenResponse:
        return TokenResponse(
            headers=self._headers,
            expires_at=datetime.now(UTC) + timedelta(hours=self._hours),
            cursor={**request.cursor, "called": True},
        )


class _FailingProvider(TokenProvider):
    async def get_token(self, request: TokenRequest) -> TokenResponse:
        raise RuntimeError("upstream auth failed")


_VALID_BODY = {
    "target": "my-target",
    "ip": "1.2.3.4",
    "port": 8080,
    "cursor": {},
    "profile": {
        "user_agent": "Mozilla/5.0",
        "accept": "text/html",
        "accept_language": "en-US",
        "accept_encoding": "gzip",
    },
}


def test_token_success():
    app = create_app(_FixedProvider({"Authorization": "Bearer abc123"}))
    with TestClient(app) as client:
        resp = client.post("/token", json=_VALID_BODY)
    assert resp.status_code == 200
    data = resp.json()
    assert data["headers"] == {"Authorization": "Bearer abc123"}
    assert data["cursor"]["called"] is True
    assert "expires_at" in data


def test_token_provider_error_returns_500():
    app = create_app(_FailingProvider())
    with TestClient(app) as client:
        resp = client.post("/token", json=_VALID_BODY)
    assert resp.status_code == 500
    assert resp.json()["detail"]["error"] == "provider_error"


def test_health():
    app = create_app(_FixedProvider({}))
    with TestClient(app) as client:
        resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_multi_target_routing():
    providers = {
        "target-a": _FixedProvider({"X-Token": "aaa"}),
        "target-b": _FixedProvider({"X-Token": "bbb"}),
    }
    app = create_app(providers)
    with TestClient(app) as client:
        resp_a = client.post("/token", json={**_VALID_BODY, "target": "target-a"})
        resp_b = client.post("/token", json={**_VALID_BODY, "target": "target-b"})
        resp_unknown = client.post("/token", json={**_VALID_BODY, "target": "unknown"})

    assert resp_a.json()["headers"]["X-Token"] == "aaa"
    assert resp_b.json()["headers"]["X-Token"] == "bbb"
    assert resp_unknown.status_code == 404


def test_cursor_passthrough():
    app = create_app(_FixedProvider({}))
    with TestClient(app) as client:
        resp = client.post("/token", json={**_VALID_BODY, "cursor": {"state": 42}})
    assert resp.json()["cursor"]["state"] == 42
    assert resp.json()["cursor"]["called"] is True
