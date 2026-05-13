"""
Example token server for proxy-hopper.

Implements the two endpoints proxy-hopper requires:
  POST /token  — issue or refresh an access token for a (target, proxy-ip) pair
  GET  /health — liveness check; must return 2xx when ready

This implementation generates self-signed JWTs to demonstrate the contract.
In production, replace _issue_token() with your real auth mechanism:

  OAuth2 client credentials
  ──────────────────────────
  async with httpx.AsyncClient() as client:
      resp = await client.post(
          "https://auth.example.com/oauth/token",
          data={
              "grant_type": "client_credentials",
              "client_id": CLIENT_ID,
              "client_secret": CLIENT_SECRET,
          },
      )
      data = resp.json()
      return data["access_token"], data["expires_in"]

  Refresh token exchange (carry the refresh token in req.cursor)
  ──────────────────────────────────────────────────────────────
  refresh_token = req.cursor.get("refresh_token")
  if refresh_token:
      # exchange it
  else:
      # first call — do a fresh login

  Session-cookie auth
  ───────────────────
  resp = await client.post("https://api.example.com/login",
                           json={"username": USER, "password": PASS})
  session_cookie = resp.cookies["session"]
  # return {"Cookie": f"session={session_cookie}"} as the headers

Environment variables:
  TOKEN_SECRET       HMAC signing secret (change this in production).
  TOKEN_TTL_MINUTES  Token lifetime in minutes (default: 60).
  PORT               Listen port (default: 9000).
  LOG_LEVEL          Logging verbosity: DEBUG | INFO | WARNING (default: INFO).
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime, timedelta

import jwt
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

TOKEN_SECRET: str = os.environ.get("TOKEN_SECRET", "dev-secret-change-me-in-production")
TOKEN_TTL: int = int(os.environ.get("TOKEN_TTL_MINUTES", "60"))
LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("token_server")

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="proxy-hopper token server example",
    description="Minimal /token + /health implementation of the proxy-hopper token server contract.",
)

# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class Profile(BaseModel):
    user_agent: str = ""
    accept: str = ""
    accept_language: str = ""
    accept_encoding: str = ""
    extra: dict = {}


class TokenRequest(BaseModel):
    """Body sent by proxy-hopper to POST /token."""

    target: str
    ip: str
    port: int
    # Opaque dict — proxy-hopper stores whatever we return in cursor and echoes
    # it back on every subsequent call for this (target, ip) pair.  Use it to
    # carry refresh tokens, session IDs, call counters, or anything else you
    # need between calls without running a database.  Empty {} on first call.
    cursor: dict = {}
    profile: Profile = Profile()
    # Included when server.authServer.exposeProxyUrl is true in config.yaml.
    proxy_url: str | None = None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.get("/health")
def health() -> dict:
    """
    Liveness check. proxy-hopper calls this at startup before pre-warming
    tokens.  Return any 2xx response when the server is ready.
    """
    return {"status": "ok"}


@app.post("/token")
def get_token(req: TokenRequest) -> dict:
    """
    Issue or refresh an access token for a (target, proxy-ip) pair.

    proxy-hopper calls this:
      - On the first request through a (target, ip) pair — cursor is {}
      - When the cached token is within refreshThresholdSeconds of expiry
      - After AUTH_BROKEN recovery (retry window elapsed)

    The returned headers are injected into every upstream request
    proxy-hopper makes through this proxy IP for this target.
    """
    now = datetime.now(UTC)
    expires_at = now + timedelta(minutes=TOKEN_TTL)

    # ── Read state from the cursor ──────────────────────────────────────────
    # cursor is {} on the first call; on subsequent calls it contains whatever
    # we returned last time.  Use it like a tiny per-IP key-value store.
    refresh_count: int = req.cursor.get("refresh_count", 0)
    is_refresh: bool = refresh_count > 0

    # ── Fetch / refresh the token ───────────────────────────────────────────
    # In this demo we generate a self-signed JWT.
    # In production: call your OAuth endpoint, exchange a refresh token, etc.
    token = _issue_token(req.target, req.ip, req.port, expires_at, req.cursor)

    # ── Build the updated cursor ────────────────────────────────────────────
    # Anything we put here is returned as-is on the next call.
    new_cursor: dict = {
        "refresh_count": refresh_count + 1,
        "last_issued": now.isoformat(),
        # In a real OAuth flow you'd store the refresh token here:
        # "refresh_token": data["refresh_token"],
    }

    logger.info(
        "%s token | target=%r ip=%s:%d ttl=%dmin refresh_count=%d expires=%s",
        "Refreshed" if is_refresh else "Issued  ",
        req.target,
        req.ip,
        req.port,
        TOKEN_TTL,
        refresh_count + 1,
        expires_at.strftime("%H:%M:%S UTC"),
    )

    return {
        # These headers are injected into the upstream request by proxy-hopper.
        # Return whatever your API requires: Authorization, Cookie, X-Api-Key, …
        "headers": {"Authorization": f"Bearer {token}"},
        # proxy-hopper refreshes the token this many seconds before this time.
        # (controlled by server.authServer.refreshThresholdSeconds in config.yaml)
        "expires_at": expires_at.isoformat(),
        # Returned as-is on the next call.
        "cursor": new_cursor,
    }


# ---------------------------------------------------------------------------
# Token generation (replace this in production)
# ---------------------------------------------------------------------------


def _issue_token(
    target: str,
    ip: str,
    port: int,
    expires_at: datetime,
    cursor: dict,
) -> str:
    """
    Generate a signed JWT.

    This is the function to replace with your real auth mechanism.

    The token returned here is ILLUSTRATIVE — it is a JWT signed with
    TOKEN_SECRET.  Upstream servers will not validate it unless you
    configure them with the matching secret.

    Real-world replacements:
      - OAuth2 client credentials → return data["access_token"]
      - Refresh-token exchange    → return data["access_token"],
                                    store data["refresh_token"] in cursor
      - Session cookie auth       → return the session cookie value,
                                    use "Cookie" instead of "Authorization"
    """
    payload = {
        "sub": f"{target}/{ip}:{port}",
        "target": target,
        "proxy_ip": ip,
        "refresh_count": cursor.get("refresh_count", 0) + 1,
        "iat": int(datetime.now(UTC).timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    return jwt.encode(payload, TOKEN_SECRET, algorithm="HS256")
