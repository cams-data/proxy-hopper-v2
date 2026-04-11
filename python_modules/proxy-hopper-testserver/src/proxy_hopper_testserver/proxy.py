"""Mock proxy layer for integration tests.

Each MockProxy instance binds to a TCP port and simulates a single external
proxy IP address.  proxy-hopper is configured to use these addresses instead
of real proxies.

Proxy failure modes
-------------------
forward         Normal operation — relay the request to the upstream server
                and return its response verbatim.  Latency can be injected
                here independently of the upstream to simulate slow network
                paths (e.g. geographically distant proxies).
refuse          Refuse the TCP connection immediately (simulates a dead proxy
                IP — proxy-hopper receives ConnectionRefusedError).
hang            Accept the TCP connection but never read or respond (simulates
                a proxy that has stopped processing — proxy-hopper's
                sock_read timeout fires).
close           Accept the TCP connection, optionally read the request, then
                close the socket without sending any response (simulates a
                proxy that crashes mid-request).
error_response  Accept the request and return a fixed HTTP error status
                (e.g. 502, 503) from the proxy itself, before reaching the
                upstream.

HTTP proxy protocol
-------------------
proxy-hopper sends plain HTTP requests in absolute-form:

    GET http://127.0.0.1:PORT/path HTTP/1.1
    Host: 127.0.0.1:PORT
    ...

The mock proxy reads the request line, extracts the upstream URL, makes the
request itself using aiohttp, and returns the response to proxy-hopper.
CONNECT tunnelling is deliberately not implemented — integration tests use
plain HTTP to keep the mock simple.

Future extension points
-----------------------
- Per-request failure injection: "fail the first N requests then go normal"
- Latency distribution: jitter around a base delay (already wired via
  self._latency — just add jitter to the sleep call)
- CONNECT tunnel simulation: accept CONNECT, establish a real TCP connection
  to the upstream, relay bytes bidirectionally
- Proxy authentication: validate Proxy-Authorization header
- Connection counters: track how many connections were accepted/refused/hung
  for assertion in tests
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

_CRLF = b"\r\n"
_HEADER_END = b"\r\n\r\n"


class ProxyMode(Enum):
    FORWARD = auto()
    REFUSE = auto()
    HANG = auto()
    CLOSE = auto()
    ERROR_RESPONSE = auto()


@dataclass
class MockProxyState:
    mode: ProxyMode = ProxyMode.FORWARD
    error_status: int = 502
    latency: float = 0.0
    # Counters
    connections_accepted: int = 0
    connections_refused: int = 0
    requests_forwarded: int = 0


class MockProxy:
    """A single mock proxy IP — binds to an ephemeral port on 127.0.0.1.

    Usage::

        async with MockProxy() as proxy:
            address = proxy.address   # "127.0.0.1:PORT" — use in ip_list
            proxy.set_mode("forward")
            proxy.set_mode("refuse")
            proxy.set_mode("hang")
            proxy.set_mode("close")
            proxy.set_mode("error_response", status=503)
            proxy.set_latency(0.5)    # simulate 500ms proxy latency
    """

    def __init__(self) -> None:
        self._state = MockProxyState()
        self._server: asyncio.Server | None = None
        self._port: int = 0
        self._session: aiohttp.ClientSession | None = None
        self._handler_tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        self._session = aiohttp.ClientSession()
        self._server = await asyncio.start_server(
            self._spawn_handler,
            host="127.0.0.1",
            port=0,
        )
        self._port = self._server.sockets[0].getsockname()[1]
        logger.debug("MockProxy: listening on %s", self.address)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
        # Cancel in-flight handlers BEFORE wait_closed() — otherwise HANG mode
        # connections keep the server open forever (wait_closed waits for all
        # connections to drop on Python 3.12+).
        for task in list(self._handler_tasks):
            task.cancel()
        if self._handler_tasks:
            await asyncio.gather(*list(self._handler_tasks), return_exceptions=True)
        if self._server:
            await self._server.wait_closed()
        if self._session:
            await self._session.close()
            self._session = None
        logger.debug("MockProxy: stopped %s", self.address)

    async def __aenter__(self) -> "MockProxy":
        await self.start()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.stop()

    # ------------------------------------------------------------------
    # Control interface
    # ------------------------------------------------------------------

    @property
    def address(self) -> str:
        return f"127.0.0.1:{self._port}"

    @property
    def connections_accepted(self) -> int:
        return self._state.connections_accepted

    @property
    def connections_refused(self) -> int:
        return self._state.connections_refused

    @property
    def requests_forwarded(self) -> int:
        return self._state.requests_forwarded

    def reset(self) -> None:
        """Reset to forward mode, zero latency, and zero all counters."""
        self._state = MockProxyState()

    def set_mode(self, mode: str, **kwargs: Any) -> None:
        """Switch the proxy's behaviour.

        Parameters
        ----------
        mode:
            One of: ``forward``, ``refuse``, ``hang``, ``close``,
            ``error_response``.
        status:
            HTTP status code (``error_response`` mode only). Default 502.
        """
        m = ProxyMode[mode.upper()]
        self._state.mode = m
        if m == ProxyMode.ERROR_RESPONSE:
            self._state.error_status = int(kwargs.get("status", 502))
        logger.debug("MockProxy %s: mode → %s %s", self.address, mode, kwargs or "")

    def set_latency(self, seconds: float) -> None:
        """Inject simulated network latency (applied before forwarding)."""
        self._state.latency = seconds
        logger.debug("MockProxy %s: latency → %.3fs", self.address, seconds)

    # ------------------------------------------------------------------
    # Connection handler
    # ------------------------------------------------------------------

    def _spawn_handler(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Spawn _handle_connection as a tracked Task so stop() can cancel it.

        asyncio.start_server calls the client_connected_cb as a plain coroutine
        (not a Task), which means we cannot cancel individual connections from
        outside.  By spawning a real Task here and tracking it, stop() can
        cancel all in-flight handlers — critical for HANG mode tests.
        """
        task = asyncio.get_running_loop().create_task(
            self._handle_connection(reader, writer)
        )
        self._handler_tasks.add(task)
        task.add_done_callback(self._handler_tasks.discard)

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        mode = self._state.mode

        # REFUSE: close the writer immediately — simulates connection refused.
        # In practice asyncio doesn't let us refuse before accept, so we
        # accept then immediately close, which the caller sees as a
        # ConnectionResetError / ServerDisconnectedError.
        if mode == ProxyMode.REFUSE:
            self._state.connections_refused += 1
            writer.close()
            return

        self._state.connections_accepted += 1

        try:
            if mode == ProxyMode.HANG:
                # Hold the connection open forever — caller's connect/read timeout fires
                await asyncio.sleep(3600)
                return

            if mode == ProxyMode.CLOSE:
                # Read just enough to look like we received something, then drop
                try:
                    await asyncio.wait_for(reader.readline(), timeout=1.0)
                except (asyncio.TimeoutError, asyncio.IncompleteReadError):
                    pass
                writer.close()
                return

            # For FORWARD and ERROR_RESPONSE we need to read the request
            method, target_url, http_version, headers = await _read_proxy_request(reader)

            if mode == ProxyMode.ERROR_RESPONSE:
                status = self._state.error_status
                reason = _reason(status)
                response = (
                    f"{http_version} {status} {reason}\r\n"
                    f"Content-Length: 0\r\n"
                    f"Connection: close\r\n"
                    f"\r\n"
                )
                writer.write(response.encode())
                await writer.drain()
                return

            # FORWARD — apply latency then proxy the request upstream
            if self._state.latency > 0:
                await asyncio.sleep(self._state.latency)

            await self._forward(method, target_url, headers, reader, writer, http_version)
            self._state.requests_forwarded += 1

        except (ConnectionResetError, asyncio.IncompleteReadError, BrokenPipeError):
            pass
        except Exception:
            logger.exception("MockProxy %s: unexpected error", self.address)
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _forward(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        http_version: str,
    ) -> None:
        """Forward the request to the upstream and relay the response back."""
        # Read body if present
        body: bytes | None = None
        content_length = headers.get("content-length")
        if content_length:
            try:
                body = await reader.readexactly(int(content_length))
            except asyncio.IncompleteReadError:
                pass

        # Strip hop-by-hop headers before forwarding
        forward_headers = {
            k: v for k, v in headers.items()
            if k.lower() not in _HOP_BY_HOP
        }

        try:
            async with self._session.request(  # type: ignore[union-attr]
                method=method,
                url=url,
                headers=forward_headers,
                data=body,
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=30.0),
            ) as resp:
                resp_body = await resp.read()
                status_line = f"{http_version} {resp.status} {_reason(resp.status)}\r\n"
                resp_headers = "".join(
                    f"{k}: {v}\r\n"
                    for k, v in resp.headers.items()
                    if k.lower() not in _HOP_BY_HOP
                )
                writer.write(
                    (status_line + resp_headers + "\r\n").encode("latin-1") + resp_body
                )
                await writer.drain()
        except aiohttp.ClientError as exc:
            logger.debug("MockProxy %s: upstream error: %s", self.address, exc)
            error = f"{http_version} 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n"
            writer.write(error.encode())
            await writer.drain()


# ---------------------------------------------------------------------------
# MockProxyPool — manages a set of mock proxies for multi-IP tests
# ---------------------------------------------------------------------------

class MockProxyPool:
    """A collection of MockProxy instances that act as a named IP pool.

    Usage::

        async with MockProxyPool(count=3) as pool:
            ip_list = pool.ip_list       # ["127.0.0.1:P1", "127.0.0.1:P2", ...]
            pool[0].set_mode("refuse")   # make first proxy fail
            pool[1].set_mode("hang")     # make second proxy hang
            pool[2].set_mode("forward")  # third proxy works normally
    """

    def __init__(self, count: int = 3) -> None:
        self._proxies: list[MockProxy] = [MockProxy() for _ in range(count)]

    async def start(self) -> None:
        for p in self._proxies:
            await p.start()

    async def stop(self) -> None:
        for p in self._proxies:
            await p.stop()

    async def __aenter__(self) -> "MockProxyPool":
        await self.start()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.stop()

    def __getitem__(self, index: int) -> MockProxy:
        return self._proxies[index]

    def __len__(self) -> int:
        return len(self._proxies)

    @property
    def ip_list(self) -> list[str]:
        return [p.address for p in self._proxies]

    def set_all_mode(self, mode: str, **kwargs: Any) -> None:
        for p in self._proxies:
            p.set_mode(mode, **kwargs)

    def set_all_latency(self, seconds: float) -> None:
        for p in self._proxies:
            p.set_latency(seconds)

    def reset_all(self) -> None:
        for p in self._proxies:
            p.reset()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "proxy-connection", "te", "trailers", "transfer-encoding", "upgrade",
})

_STATUS_REASONS: dict[int, str] = {
    200: "OK", 201: "Created", 204: "No Content",
    400: "Bad Request", 401: "Unauthorized", 403: "Forbidden",
    404: "Not Found", 429: "Too Many Requests",
    500: "Internal Server Error", 502: "Bad Gateway",
    503: "Service Unavailable", 504: "Gateway Timeout",
}


def _reason(status: int) -> str:
    return _STATUS_REASONS.get(status, "Unknown")


async def _read_proxy_request(
    reader: asyncio.StreamReader,
) -> tuple[str, str, str, dict[str, str]]:
    """Read an HTTP proxy request line and headers from the stream."""
    raw_line = await asyncio.wait_for(reader.readline(), timeout=5.0)
    if not raw_line:
        raise asyncio.IncompleteReadError(b"", None)

    request_line = raw_line.decode("latin-1").rstrip("\r\n")
    parts = request_line.split(" ", 2)
    if len(parts) != 3:
        raise ValueError(f"Malformed request line: {request_line!r}")
    method, target_url, version = parts

    headers: dict[str, str] = {}
    while True:
        line = await asyncio.wait_for(reader.readline(), timeout=5.0)
        if line in (_CRLF, b"\n", b""):
            break
        decoded = line.decode("latin-1").rstrip("\r\n")
        if ":" in decoded:
            name, _, value = decoded.partition(":")
            headers[name.strip().lower()] = value.strip()

    return method, target_url, version, headers
