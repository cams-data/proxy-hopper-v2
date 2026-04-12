"""Tests for the RequestHandler abstraction and ForwardingHandler."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from proxy_hopper.backend.memory import MemoryIPPoolBackend
from proxy_hopper.config import TargetConfig
from test_helpers import make_target_config as _make_target_config
from proxy_hopper.handlers import (
    ForwardingHandler,
    _build_handlers,
    _find_first_manager,
)
from proxy_hopper.models import PendingRequest, ProxyResponse
from proxy_hopper.server import ProxyServer
from proxy_hopper.target_manager import TargetManager


def make_manager(regex: str = r".*example\.com.*", name: str = "test") -> TargetManager:
    cfg = _make_target_config(
        ["1.2.3.4:8080"],
        name=name,
        regex=regex,
        min_request_interval=0.0,
        max_queue_wait=2.0,
        num_retries=1,
        ip_failures_until_quarantine=3,
        quarantine_time=0.1,
    )
    return TargetManager(cfg, MemoryIPPoolBackend())


# ---------------------------------------------------------------------------
# can_handle routing
# ---------------------------------------------------------------------------

class TestCanHandle:
    def test_forwarding_claims_request_with_header(self):
        h = ForwardingHandler([])
        assert h.can_handle("GET", "/v1/data", "HTTP/1.1", {"x-proxy-hopper-target": "https://api.example.com"})

    def test_forwarding_claims_any_method_with_header(self):
        h = ForwardingHandler([])
        assert h.can_handle("POST", "/v1/data", "HTTP/1.1", {"x-proxy-hopper-target": "https://api.example.com"})

    def test_forwarding_ignores_request_without_header(self):
        h = ForwardingHandler([])
        assert not h.can_handle("GET", "/v1/data", "HTTP/1.1", {})

    def test_forwarding_ignores_absolute_url_without_header(self):
        h = ForwardingHandler([])
        assert not h.can_handle("GET", "https://example.com/", "HTTP/1.1", {})


# ---------------------------------------------------------------------------
# _build_handlers ordering and filtering
# ---------------------------------------------------------------------------

class TestBuildHandlers:
    def test_default_returns_forwarding_handler(self):
        handlers = _build_handlers([])
        assert len(handlers) == 1
        assert isinstance(handlers[0], ForwardingHandler)

    def test_forwarding_mode_explicit(self):
        handlers = _build_handlers([], {"forwarding"})
        assert len(handlers) == 1
        assert isinstance(handlers[0], ForwardingHandler)

    def test_empty_modes_returns_no_handlers(self):
        assert _build_handlers([], set()) == []


# ---------------------------------------------------------------------------
# _find_first_manager
# ---------------------------------------------------------------------------

class TestFindFirstManager:
    def test_returns_matching_manager(self):
        mgr = make_manager(r".*example\.com.*")
        assert _find_first_manager([mgr], "http://example.com/") is mgr

    def test_returns_none_when_no_match(self):
        mgr = make_manager(r".*example\.com.*")
        assert _find_first_manager([mgr], "http://other.com/") is None

    def test_returns_first_match(self):
        m1 = make_manager(r".*example\.com.*", name="first")
        m2 = make_manager(r".*example\.com.*", name="second")
        assert _find_first_manager([m1, m2], "http://example.com/") is m1


# ---------------------------------------------------------------------------
# ForwardingHandler integration via ProxyServer
# ---------------------------------------------------------------------------

class TestForwardingHandlerIntegration:
    async def _start_server(self, managers, modes=None) -> tuple[ProxyServer, int]:
        server = ProxyServer(
            managers,
            host="127.0.0.1",
            port=0,
            enabled_modes=modes,
        )
        server._server = await asyncio.start_server(
            server._handle_client, host="127.0.0.1", port=0
        )
        port = server._server.sockets[0].getsockname()[1]
        return server, port

    async def _close(self, server: ProxyServer) -> None:
        server._server.close()
        await server._server.wait_closed()

    async def test_forwarding_reconstructs_url_and_routes(self):
        mgr = make_manager(r".*example\.com.*")
        fake_response = ProxyResponse(
            status=200, headers={"content-type": "text/plain"}, body=b"forwarded"
        )
        submitted: list[PendingRequest] = []

        async def fake_submit(req: PendingRequest) -> None:
            submitted.append(req)
            if not req.future.done():
                req.future.set_result(fake_response)

        with patch.object(mgr, "submit", side_effect=fake_submit):
            server, port = await self._start_server([mgr])
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(
                b"GET /api/data?q=1 HTTP/1.1\r\n"
                b"Host: localhost:8080\r\n"
                b"X-Proxy-Hopper-Target: https://example.com\r\n\r\n"
            )
            await writer.drain()
            data = await asyncio.wait_for(reader.read(4096), timeout=2.0)
            writer.close()

        assert b"200" in data
        assert b"forwarded" in data
        assert len(submitted) == 1
        assert submitted[0].url == "https://example.com/api/data?q=1"
        await self._close(server)

    async def test_forwarding_rewrites_host_header(self):
        mgr = make_manager(r".*example\.com.*")
        submitted: list[PendingRequest] = []

        async def fake_submit(req: PendingRequest) -> None:
            submitted.append(req)
            req.future.set_result(ProxyResponse(status=200, headers={}, body=b"ok"))

        with patch.object(mgr, "submit", side_effect=fake_submit):
            server, port = await self._start_server([mgr])
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(
                b"GET /path HTTP/1.1\r\n"
                b"Host: proxy-hopper:8080\r\n"
                b"X-Proxy-Hopper-Target: https://example.com\r\n\r\n"
            )
            await writer.drain()
            await asyncio.wait_for(reader.read(4096), timeout=2.0)
            writer.close()

        assert submitted[0].headers["host"] == "example.com"
        assert "x-proxy-hopper-target" not in submitted[0].headers
        await self._close(server)

    async def test_forwarding_strips_all_proxy_hopper_headers(self):
        """All X-Proxy-Hopper-* headers must be stripped before upstream."""
        mgr = make_manager(r".*example\.com.*")
        submitted: list[PendingRequest] = []

        async def fake_submit(req: PendingRequest) -> None:
            submitted.append(req)
            req.future.set_result(ProxyResponse(status=200, headers={}, body=b"ok"))

        with patch.object(mgr, "submit", side_effect=fake_submit):
            server, port = await self._start_server([mgr])
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(
                b"GET /path HTTP/1.1\r\n"
                b"Host: proxy-hopper:8080\r\n"
                b"X-Proxy-Hopper-Target: https://example.com\r\n"
                b"X-Proxy-Hopper-Tag: search\r\n"
                b"X-Proxy-Hopper-Retries: 5\r\n\r\n"
            )
            await writer.drain()
            await asyncio.wait_for(reader.read(4096), timeout=2.0)
            writer.close()

        h = submitted[0].headers
        assert "x-proxy-hopper-target" not in h
        assert "x-proxy-hopper-tag" not in h
        assert "x-proxy-hopper-retries" not in h
        await self._close(server)

    async def test_forwarding_unmatched_target_returns_502(self):
        mgr = make_manager(r".*example\.com.*")
        server, port = await self._start_server([mgr])
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
        await self._close(server)

    async def test_no_target_header_returns_400(self):
        """Requests without X-Proxy-Hopper-Target get a 400."""
        mgr = make_manager(r".*example\.com.*")
        server, port = await self._start_server([mgr])
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            b"GET /path HTTP/1.1\r\n"
            b"Host: localhost:8080\r\n\r\n"
        )
        await writer.drain()
        data = await asyncio.wait_for(reader.read(4096), timeout=2.0)
        writer.close()
        assert b"400" in data
        await self._close(server)

    async def test_forwarding_header_with_base_path(self):
        """Target header may include a base path prefix."""
        mgr = make_manager(r".*example\.com.*")
        submitted: list[PendingRequest] = []

        async def fake_submit(req: PendingRequest) -> None:
            submitted.append(req)
            req.future.set_result(ProxyResponse(status=200, headers={}, body=b"ok"))

        with patch.object(mgr, "submit", side_effect=fake_submit):
            server, port = await self._start_server([mgr])
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(
                b"GET /endpoint HTTP/1.1\r\n"
                b"Host: localhost:8080\r\n"
                b"X-Proxy-Hopper-Target: https://example.com/api/v1\r\n\r\n"
            )
            await writer.drain()
            await asyncio.wait_for(reader.read(4096), timeout=2.0)
            writer.close()

        assert submitted[0].url == "https://example.com/api/v1/endpoint"
        await self._close(server)


# ---------------------------------------------------------------------------
# X-Proxy-Hopper-Tag propagation
# ---------------------------------------------------------------------------

class TestTagHeader:
    async def _submit_with_headers(self, extra_headers: bytes) -> PendingRequest:
        mgr = make_manager(r".*example\.com.*")
        submitted: list[PendingRequest] = []

        async def fake_submit(req: PendingRequest) -> None:
            submitted.append(req)
            req.future.set_result(ProxyResponse(status=200, headers={}, body=b"ok"))

        server = ProxyServer([mgr], host="127.0.0.1", port=0)
        server._server = await asyncio.start_server(
            server._handle_client, host="127.0.0.1", port=0
        )
        port = server._server.sockets[0].getsockname()[1]

        with patch.object(mgr, "submit", side_effect=fake_submit):
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(
                b"GET /path HTTP/1.1\r\n"
                b"Host: localhost:8080\r\n"
                b"X-Proxy-Hopper-Target: https://example.com\r\n"
                + extra_headers + b"\r\n"
            )
            await writer.drain()
            await asyncio.wait_for(reader.read(4096), timeout=2.0)
            writer.close()

        server._server.close()
        await server._server.wait_closed()
        return submitted[0]

    async def test_tag_propagated_to_pending_request(self):
        req = await self._submit_with_headers(b"X-Proxy-Hopper-Tag: search\r\n")
        assert req.tag == "search"

    async def test_empty_tag_when_header_absent(self):
        req = await self._submit_with_headers(b"")
        assert req.tag == ""

    async def test_tag_preserved_through_clone_for_retry(self):
        req = await self._submit_with_headers(b"X-Proxy-Hopper-Tag: my-endpoint\r\n")
        retried = req.clone_for_retry()
        assert retried.tag == "my-endpoint"


# ---------------------------------------------------------------------------
# X-Proxy-Hopper-Retries override
# ---------------------------------------------------------------------------

class TestRetriesHeader:
    async def _submit_with_retries_header(self, value: bytes) -> PendingRequest:
        mgr = make_manager(r".*example\.com.*")
        submitted: list[PendingRequest] = []

        async def fake_submit(req: PendingRequest) -> None:
            submitted.append(req)
            req.future.set_result(ProxyResponse(status=200, headers={}, body=b"ok"))

        server = ProxyServer([mgr], host="127.0.0.1", port=0)
        server._server = await asyncio.start_server(
            server._handle_client, host="127.0.0.1", port=0
        )
        port = server._server.sockets[0].getsockname()[1]

        with patch.object(mgr, "submit", side_effect=fake_submit):
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(
                b"GET /path HTTP/1.1\r\n"
                b"Host: localhost:8080\r\n"
                b"X-Proxy-Hopper-Target: https://example.com\r\n"
                b"X-Proxy-Hopper-Retries: " + value + b"\r\n\r\n"
            )
            await writer.drain()
            await asyncio.wait_for(reader.read(4096), timeout=2.0)
            writer.close()

        server._server.close()
        await server._server.wait_closed()
        return submitted[0]

    async def test_retries_override_applied(self):
        req = await self._submit_with_retries_header(b"7")
        assert req.num_retries == 7

    async def test_zero_retries_allowed(self):
        req = await self._submit_with_retries_header(b"0")
        assert req.num_retries == 0

    async def test_invalid_retries_value_uses_target_default(self):
        req = await self._submit_with_retries_header(b"notanumber")
        # make_manager sets num_retries=1 as the target default
        assert req.num_retries == 1

    async def test_negative_retries_ignored(self):
        req = await self._submit_with_retries_header(b"-3")
        assert req.num_retries == 1  # target default

    async def test_retries_absent_uses_target_default(self):
        mgr = make_manager(r".*example\.com.*")
        submitted: list[PendingRequest] = []

        async def fake_submit(req: PendingRequest) -> None:
            submitted.append(req)
            req.future.set_result(ProxyResponse(status=200, headers={}, body=b"ok"))

        server = ProxyServer([mgr], host="127.0.0.1", port=0)
        server._server = await asyncio.start_server(
            server._handle_client, host="127.0.0.1", port=0
        )
        port = server._server.sockets[0].getsockname()[1]

        with patch.object(mgr, "submit", side_effect=fake_submit):
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(
                b"GET /path HTTP/1.1\r\n"
                b"Host: localhost:8080\r\n"
                b"X-Proxy-Hopper-Target: https://example.com\r\n\r\n"
            )
            await writer.drain()
            await asyncio.wait_for(reader.read(4096), timeout=2.0)
            writer.close()

        server._server.close()
        await server._server.wait_closed()
        assert submitted[0].num_retries == 1  # target default
