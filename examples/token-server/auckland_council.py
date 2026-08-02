"""Example token server implementation — Auckland Council.

Demonstrates how to write a TokenProvider that:
- Routes the auth request through the same proxy IP that will use the token
  (IP-pinned acquisition via ProxyHopperClient).
- Carries a session cookie across refreshes using the cursor.

This file is a standalone reference example — unlike the FastAPI demo in
token_server/, it depends on the proxy_hopper_token_server library itself
(see the `dev` extra in this directory's pyproject.toml, which installs it
as a local editable path dependency).

Run with (from this directory):
    uv sync --extra dev
    uv run ph-token-server start auckland_council:provider

Requires a running Proxy Hopper instance with `server.authServer.exposeProxyUrl:
true` set, since ProxyHopperClient needs `req.proxy_url` to route the token
request through a specific pinned IP.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from proxy_hopper_token_server import (
    ProxyHopperClient,
    TokenProvider,
    TokenRequest,
    TokenResponse,
)

# Next-action hash for the search endpoint — update if the site changes.
_NEXT_ACTION = "0085094deec5539b4c1957dddebcf440d92db43f11"
_TOKEN_RE = re.compile(r'"token"\s*:\s*"([^"]+)"')


class AucklandCouncilTokens(TokenProvider):
    async def get_token(self, req: TokenRequest) -> TokenResponse:
        client = ProxyHopperClient(proxy_url=req.proxy_url)
        via = f"{req.ip}:{req.port}"

        # If we have a session cookie from a previous call, pass it along.
        headers: dict[str, str] = {
            "User-Agent": req.profile.user_agent,
            "Accept": req.profile.accept,
            "Accept-Language": req.profile.accept_language,
            "Accept-Encoding": req.profile.accept_encoding,
            "accept": "text/x-component",
            "next-action": _NEXT_ACTION,
            "content-type": "application/json",
        }
        if session_cookie := req.cursor.get("session_cookie"):
            headers["Cookie"] = session_cookie

        resp = await client.post(
            "https://www.aucklandcouncil.govt.nz/en/property-rates-valuations/find-property.html",
            via_ip=via,
            headers=headers,
            data=b"[]",
            timeout=20.0,
        )
        body = await resp.read()

        match = _TOKEN_RE.search(body.decode("utf-8", errors="replace"))
        if not match:
            raise ValueError(f"Could not extract token from response (status={resp.status})")

        token = match.group(1)

        # Persist session cookies for the next refresh.
        set_cookie = resp.headers.get("Set-Cookie", "")
        cursor = {"session_cookie": set_cookie} if set_cookie else req.cursor

        return TokenResponse(
            headers={"Authorization": f"Bearer {token}"},
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            cursor=cursor,
        )


# Single instance — passed to ph-token-server via:
#   uv run ph-token-server start auckland_council:provider
provider = AucklandCouncilTokens()
