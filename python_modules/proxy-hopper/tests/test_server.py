"""Tests for the TCP proxy server."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, patch

import pytest

from proxy_hopper.backend.memory import MemoryBackend
from proxy_hopper.config import TargetConfig
from proxy_hopper.pool_store import IPPoolStore
from test_helpers import make_target_config as _make_target_config
from proxy_hopper.models import PendingRequest, ProxyResponse
from proxy_hopper.server import (
    ProxyServer,
    _read_request_head,
    _reason,
)
from proxy_hopper.target_manager import TargetManager


def make_target_config(regex: str = r".*example\.com.*") -> TargetConfig:
    return _make_target_config(
        ["1.2.3.4:8080"],
        name="test",
        regex=regex,
        min_request_interval=0.0,
        max_queue_wait=2.0,
        num_retries=1,
        ip_failures_until_quarantine=3,
        quarantine_time=0.1,
    )


# ---------------------------------------------------------------------------
# _read_request_head
# ---------------------------------------------------------------------------

class TestReadRequestHead:
    async def test_parses_get(self):
        raw = b"GET http://example.com/path HTTP/1.1\r\nHost: example.com\r\nAccept: */*\r\n\r\n"
        reader = asyncio.StreamReader()
        reader.feed_data(raw)
        method, target, version, headers = await _read_request_head(reader)
        assert method == "GET"
        assert target == "http://example.com/path"
        assert version == "HTTP/1.1"
        assert headers["host"] == "example.com"
        assert headers["accept"] == "*/*"

    async def test_parses_connect(self):
        raw = b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n"
        reader = asyncio.StreamReader()
        reader.feed_data(raw)
        method, target, version, _ = await _read_request_head(reader)
        assert method == "CONNECT"
        assert target == "example.com:443"

    async def test_malformed_line_raises(self):
        raw = b"BADLINE\r\n\r\n"
        reader = asyncio.StreamReader()
        reader.feed_data(raw)
        with pytest.raises(ValueError, match="Malformed"):
            await _read_request_head(reader)

    async def test_header_case_normalised(self):
        raw = b"GET http://example.com/ HTTP/1.1\r\nContent-Type: application/json\r\n\r\n"
        reader = asyncio.StreamReader()
        reader.feed_data(raw)
        _, _, _, headers = await _read_request_head(reader)
        assert "content-type" in headers


# ---------------------------------------------------------------------------
# _reason helper
# ---------------------------------------------------------------------------

class TestReason:
    def test_known_codes(self):
        assert _reason(200) == "OK"
        assert _reason(404) == "Not Found"
        assert _reason(504) == "Gateway Timeout"
        assert _reason(429) == "Too Many Requests"

    def test_unknown_code(self):
        assert _reason(999) == "Unknown"


# ---------------------------------------------------------------------------
# ProxyServer._find_manager
# ---------------------------------------------------------------------------

class TestFindManager:
    def _make_server(self, *regexes):
        managers = []
        for regex in regexes:
            cfg = make_target_config(regex=regex)
            backend = IPPoolStore(MemoryBackend())
            managers.append(TargetManager(cfg, backend))
        return ProxyServer(managers)

    def test_returns_matching_manager(self):
        server = self._make_server(r".*example\.com.*")
        mgr = server._find_manager("http://example.com/path")
        assert mgr is not None
        assert mgr._config.name == "test"

    def test_returns_none_for_no_match(self):
        server = self._make_server(r".*example\.com.*")
        assert server._find_manager("http://other.com/path") is None

    def test_returns_first_match(self):
        cfg1 = make_target_config(r".*example\.com.*")
        cfg1 = TargetConfig(**{**cfg1.model_dump(), "name": "first"})
        cfg2 = TargetConfig(**{**make_target_config(r".*example\.com.*").model_dump(), "name": "second"})
        b1, b2 = IPPoolStore(MemoryBackend()), IPPoolStore(MemoryBackend())
        m1, m2 = TargetManager(cfg1, b1), TargetManager(cfg2, b2)
        server = ProxyServer([m1, m2])
        assert server._find_manager("http://example.com/") is m1


# ---------------------------------------------------------------------------
# Integration: unmatched URL returns 502
# ---------------------------------------------------------------------------

class TestProxyServerHTTP:
    async def _start_server_on_free_port(self, managers) -> tuple[ProxyServer, int]:
        """Start server, bypass manager.start() internals, return (server, port)."""
        server = ProxyServer(managers, host="127.0.0.1", port=0)
        # Start the TCP listener without calling manager.start so we can mock submit
        server._server = await asyncio.start_server(
            server._handle_client, host="127.0.0.1", port=0
        )
        port = server._server.sockets[0].getsockname()[1]
        return server, port

    async def test_unmatched_target_returns_502(self):
        cfg = make_target_config(r".*example\.com.*")
        backend = IPPoolStore(MemoryBackend())
        mgr = TargetManager(cfg, backend)
        server, port = await self._start_server_on_free_port([mgr])

        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            b"GET /path HTTP/1.1\r\n"
            b"Host: localhost:8080\r\n"
            b"X-Proxy-Hopper-Target: https://notmatched.org\r\n\r\n"
        )
        await writer.drain()

        data = await asyncio.wait_for(reader.read(4096), timeout=2.0)
        writer.close()
        assert b"502" in data

        server._server.close()
        await server._server.wait_closed()

    async def test_matched_target_routes_to_manager(self):
        cfg = make_target_config(r".*example\.com.*")
        backend = IPPoolStore(MemoryBackend())
        mgr = TargetManager(cfg, backend)

        fake_response = ProxyResponse(
            status=200, headers={"content-type": "text/plain"}, body=b"proxied"
        )

        async def fake_submit(request: PendingRequest) -> None:
            if not request.future.done():
                request.future.set_result(fake_response)

        with patch.object(mgr, "submit", side_effect=fake_submit):
            server, port = await self._start_server_on_free_port([mgr])

            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(
                b"GET /path HTTP/1.1\r\n"
                b"Host: localhost:8080\r\n"
                b"X-Proxy-Hopper-Target: https://example.com\r\n\r\n"
            )
            await writer.drain()

            data = await asyncio.wait_for(reader.read(4096), timeout=2.0)
            writer.close()
            assert b"200" in data
            assert b"proxied" in data

            server._server.close()
            await server._server.wait_closed()


# ---------------------------------------------------------------------------
# Full lifecycle: start / stop
# ---------------------------------------------------------------------------

class TestProxyServerLifecycle:
    async def test_start_and_stop(self):
        cfg = make_target_config()
        backend = IPPoolStore(MemoryBackend())
        mgr = TargetManager(cfg, backend)
        server = ProxyServer([mgr], host="127.0.0.1", port=0)
        await server.start()
        assert server._server is not None
        assert server._server.is_serving()
        await server.stop()
        assert not server._server.is_serving()
