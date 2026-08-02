"""Tests for ProxyHopperClient.

These run a tiny aiohttp server standing in for Proxy Hopper's forwarding-mode
listener, and assert ProxyHopperClient builds a header-based forwarding
request (X-Proxy-Hopper-Target + X-ProxyHopper-Force-IP sent directly to the
proxy's own address) rather than a classic HTTP-proxy/CONNECT request — the
bug this client used to have, since Proxy Hopper no longer supports those
modes at all.
"""

from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from proxy_hopper_token_server.client import ProxyHopperClient


@pytest.fixture
async def fake_proxy_hopper():
    """A minimal server standing in for Proxy Hopper. Records the last
    request it received and returns a fixed response."""
    received: dict = {}

    async def handler(request: web.Request) -> web.Response:
        received["method"] = request.method
        received["path"] = request.path
        received["query"] = request.query_string
        received["headers"] = dict(request.headers)
        received["body"] = await request.read()
        return web.Response(status=200, body=b'{"ok": true}', headers={"X-Test": "yes"})

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    server = TestServer(app)
    await server.start_server()
    try:
        yield server, received
    finally:
        await server.close()


async def test_request_uses_forwarding_mode_headers(fake_proxy_hopper):
    server, received = fake_proxy_hopper
    client = ProxyHopperClient(proxy_url=f"http://{server.host}:{server.port}")

    resp = await client.post(
        "https://example.com/auth/token?x=1",
        via_ip="10.0.0.5:3128",
        headers={"User-Agent": "test-agent"},
        data=b'{"grant_type": "client_credentials"}',
    )

    assert resp.status == 200
    assert resp.headers["X-Test"] == "yes"
    assert await resp.read() == b'{"ok": true}'

    # The request must be a plain request sent directly to Proxy Hopper's own
    # address, not a CONNECT tunnel or absolute-URI proxy request.
    assert received["method"] == "POST"
    assert received["path"] == "/auth/token"
    assert received["query"] == "x=1"
    assert received["body"] == b'{"grant_type": "client_credentials"}'
    assert received["headers"]["X-Proxy-Hopper-Target"] == "https://example.com"
    assert received["headers"]["X-ProxyHopper-Force-IP"] == "10.0.0.5:3128"
    assert received["headers"]["User-Agent"] == "test-agent"


async def test_get_uses_root_path_when_url_has_no_path(fake_proxy_hopper):
    server, received = fake_proxy_hopper
    client = ProxyHopperClient(proxy_url=f"http://{server.host}:{server.port}")

    await client.get("https://example.com", via_ip="1.2.3.4:8080")

    assert received["path"] == "/"
    assert received["headers"]["X-Proxy-Hopper-Target"] == "https://example.com"


async def test_target_header_excludes_path(fake_proxy_hopper):
    """X-Proxy-Hopper-Target must be scheme+host only — the path is sent as
    the actual request path, matching ForwardingHandler's reconstruction
    logic (target + path)."""
    server, received = fake_proxy_hopper
    client = ProxyHopperClient(proxy_url=f"http://{server.host}:{server.port}")

    await client.get("https://example.com/v1/data", via_ip="1.2.3.4:8080")

    assert received["path"] == "/v1/data"
    assert received["headers"]["X-Proxy-Hopper-Target"] == "https://example.com"


async def test_response_readable_after_call_returns(fake_proxy_hopper):
    """Regression test: the previous implementation returned the raw
    aiohttp.ClientResponse after closing its ClientSession, so reading the
    body afterwards was unreliable. ProxyHopperResponse buffers the body
    eagerly, so read() must work any number of times with no live connection."""
    server, _ = fake_proxy_hopper
    client = ProxyHopperClient(proxy_url=f"http://{server.host}:{server.port}")

    resp = await client.get("https://example.com/", via_ip="1.2.3.4:8080")

    assert await resp.read() == b'{"ok": true}'
    assert await resp.read() == b'{"ok": true}'  # safe to call twice
    assert resp.text() == '{"ok": true}'
