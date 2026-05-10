"""Tests for proxy_hopper_webserver.app (FastAPI endpoints)."""

from __future__ import annotations

import secrets
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from proxy_hopper.auth import hash_password
from proxy_hopper.config import load_config
from proxy_hopper_webserver.app import create_admin_app

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ADMIN_PASSWORD = "hunter2"
_ADMIN_HASH = hash_password(_ADMIN_PASSWORD)

_CONFIG_NO_AUTH = """\
proxyProviders:
  - name: p
    ipList: ["1.2.3.4:3128"]
ipPools:
  - name: pool
    ipRequests:
      - provider: p
        count: 1
targets:
  - name: example
    regex: '.*example\\.com.*'
    ipPool: pool
    minRequestInterval: 1s
    maxQueueWait: 5s
    numRetries: 2
    ipFailuresUntilQuarantine: 3
    quarantineTime: 30s
    defaultProxyPort: 3128
"""

_CONFIG_WITH_AUTH = f"""\
proxyProviders:
  - name: p
    ipList: ["1.2.3.4:3128"]
ipPools:
  - name: pool
    ipRequests:
      - provider: p
        count: 1
targets:
  - name: example
    regex: '.*example\\.com.*'
    ipPool: pool
    minRequestInterval: 1s
    maxQueueWait: 5s
    numRetries: 2
    ipFailuresUntilQuarantine: 3
    quarantineTime: 30s
    defaultProxyPort: 3128
auth:
  enabled: true
  admin:
    username: admin
    passwordHash: "{_ADMIN_HASH}"
    role: admin
"""


def _cfg_from_yaml(yaml: str, tmp_path: Path):
    p = tmp_path / "config.yaml"
    p.write_text(yaml)
    return load_config(str(p))


def _make_client(yaml: str, tmp_path: Path) -> TestClient:
    cfg = _cfg_from_yaml(yaml, tmp_path)
    secret = secrets.token_hex(32)
    app = create_admin_app(cfg, runtime_secret=secret)
    return TestClient(app, raise_server_exceptions=True)


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------


def test_health(tmp_path):
    client = _make_client(_CONFIG_NO_AUTH, tmp_path)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# /api/v1/status — auth disabled
# ---------------------------------------------------------------------------


def test_status_no_auth(tmp_path):
    client = _make_client(_CONFIG_NO_AUTH, tmp_path)
    resp = client.get("/api/v1/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["auth_enabled"] is False
    assert data["user"]["sub"] == "anonymous"
    assert any(t["name"] == "example" for t in data["targets"])


# ---------------------------------------------------------------------------
# /api/v1/status — auth enabled
# ---------------------------------------------------------------------------


def test_status_requires_auth(tmp_path):
    client = _make_client(_CONFIG_WITH_AUTH, tmp_path)
    resp = client.get("/api/v1/status")
    assert resp.status_code == 401


def test_status_with_valid_token(tmp_path):
    cfg = _cfg_from_yaml(_CONFIG_WITH_AUTH, tmp_path)
    secret = secrets.token_hex(32)
    app = create_admin_app(cfg, runtime_secret=secret)
    client = TestClient(app)

    token_resp = client.post(
        "/auth/login",
        data={"username": "admin", "password": _ADMIN_PASSWORD},
    )
    assert token_resp.status_code == 200
    token = token_resp.json()["access_token"]

    resp = client.get("/api/v1/status", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["auth_enabled"] is True


# ---------------------------------------------------------------------------
# /auth/login
# ---------------------------------------------------------------------------


def test_login_no_auth_config_returns_404(tmp_path):
    client = _make_client(_CONFIG_NO_AUTH, tmp_path)
    resp = client.post(
        "/auth/login",
        data={"username": "admin", "password": "anything"},
    )
    assert resp.status_code == 404


def test_login_wrong_password(tmp_path):
    client = _make_client(_CONFIG_WITH_AUTH, tmp_path)
    resp = client.post(
        "/auth/login",
        data={"username": "admin", "password": "wrongpass"},
    )
    assert resp.status_code == 401


def test_login_wrong_username(tmp_path):
    client = _make_client(_CONFIG_WITH_AUTH, tmp_path)
    resp = client.post(
        "/auth/login",
        data={"username": "notadmin", "password": _ADMIN_PASSWORD},
    )
    assert resp.status_code == 401


def test_login_success_returns_bearer_token(tmp_path):
    client = _make_client(_CONFIG_WITH_AUTH, tmp_path)
    resp = client.post(
        "/auth/login",
        data={"username": "admin", "password": _ADMIN_PASSWORD},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["token_type"] == "bearer"
    assert len(body["access_token"]) > 20


# ---------------------------------------------------------------------------
# /auth/login — token can be used for authenticated endpoints
# ---------------------------------------------------------------------------


def test_token_grants_status_access(tmp_path):
    cfg = _cfg_from_yaml(_CONFIG_WITH_AUTH, tmp_path)
    secret = secrets.token_hex(32)
    app = create_admin_app(cfg, runtime_secret=secret)
    client = TestClient(app)

    token = client.post(
        "/auth/login",
        data={"username": "admin", "password": _ADMIN_PASSWORD},
    ).json()["access_token"]

    resp = client.get("/api/v1/status", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200


def test_tampered_token_rejected(tmp_path):
    cfg = _cfg_from_yaml(_CONFIG_WITH_AUTH, tmp_path)
    secret = secrets.token_hex(32)
    app = create_admin_app(cfg, runtime_secret=secret)
    client = TestClient(app)

    token = client.post(
        "/auth/login",
        data={"username": "admin", "password": _ADMIN_PASSWORD},
    ).json()["access_token"]

    bad_token = token[:-4] + "XXXX"
    resp = client.get("/api/v1/status", headers={"Authorization": f"Bearer {bad_token}"})
    assert resp.status_code == 401
