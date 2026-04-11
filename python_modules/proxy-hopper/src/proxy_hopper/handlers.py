"""Request handler abstraction for Proxy Hopper.

Each supported interaction mode (HTTP proxy, CONNECT tunnel, URL-forwarding)
is implemented as a concrete ``RequestHandler``.  ``ProxyServer._dispatch``
iterates the registered handlers in priority order, delegates to the first
one whose ``can_handle`` returns True, and returns a 400 if nothing matches.

Adding a new mode
-----------------
1. Subclass ``RequestHandler`` and implement ``can_handle`` + ``handle``.
2. Add the class to ``_MODE_REGISTRY`` with a string key.
3. Add the key to the ordered list in ``_build_handlers``.
4. Add the key to ``ServerConfig.modes`` validator in ``config.py``.

That is the complete change surface — ``ProxyServer`` and ``_dispatch`` need
no modification.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import aiohttp

from .metrics import get_metrics
from .models import PendingRequest, ProxyResponse

if TYPE_CHECKING:
    from .target_manager import TargetManager

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

_MAX_BODY_SIZE = 65_536 * 160   # 10 MiB
_HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "proxy-connection", "te", "trailers", "transfer-encoding", "upgrade",
})

# ---------------------------------------------------------------------------
# Low-level I/O helpers
# ---------------------------------------------------------------------------

_REASONS: dict[int, str] = {
    200: "OK", 201: "Created", 204: "No Content",
    301: "Moved Permanently", 302: "Found", 304: "Not Modified",
    400: "Bad Request", 401: "Unauthorized", 403: "Forbidden",
    404: "Not Found", 405: "Method Not Allowed", 408: "Request Timeout",
    413: "Request Entity Too Large", 429: "Too Many Requests",
    500: "Internal Server Error", 502: "Bad Gateway",
    503: "Service Unavailable", 504: "Gateway Timeout",
}


def _reason(status: int) -> str:
    return _REASONS.get(status, "Unknown")


def _write_error(writer: asyncio.StreamWriter, status: int, message: str) -> None:
    body = message.encode()
    writer.write(
        (
            f"HTTP/1.1 {status} {_reason(status)}\r\n"
            f"Content-Type: text/plain\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Connection: close\r\n"
            "\r\n"
        ).encode() + body
    )


def _write_raw(writer: asyncio.StreamWriter, data: bytes) -> None:
    writer.write(data)


def _write_http_response(
    writer: asyncio.StreamWriter,
    response: ProxyResponse,
    http_version: str,
) -> None:
    status_line = f"{http_version} {response.status} {_reason(response.status)}\r\n"
    header_lines = "".join(
        f"{k}: {v}\r\n"
        for k, v in response.headers.items()
        if k.lower() not in _HOP_BY_HOP
    )
    writer.write((status_line + header_lines + "\r\n").encode("latin-1") + response.body)


async def _relay(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    proxy_reader: asyncio.StreamReader,
    proxy_writer: asyncio.StreamWriter,
    chunk: int = 65_536,
) -> None:
    async def pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
        try:
            while True:
                data = await src.read(chunk)
                if not data:
                    break
                dst.write(data)
                await dst.drain()
        except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError):
            pass
        finally:
            try:
                dst.close()
            except Exception:
                pass

    await asyncio.gather(
        pipe(client_reader, proxy_writer),
        pipe(proxy_reader, client_writer),
        return_exceptions=True,
    )


def _build_connect_request(
    target: str,
    username: str | None,
    password: str | None,
) -> bytes:
    headers = f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n"
    if username is not None:
        creds = base64.b64encode(f"{username}:{password or ''}".encode()).decode()
        headers += f"Proxy-Authorization: Basic {creds}\r\n"
    headers += "\r\n"
    return headers.encode()


# ---------------------------------------------------------------------------
# Shared request-dispatch helper (used by HTTP proxy and forwarding modes)
# ---------------------------------------------------------------------------

def _find_first_manager(
    managers: list[TargetManager], url: str
) -> TargetManager | None:
    return next((m for m in managers if m.matches(url)), None)


async def _submit_and_respond(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    method: str,
    url: str,
    http_version: str,
    headers: dict[str, str],
    managers: list[TargetManager],
) -> None:
    """Read body → find manager → submit → await response → write reply.

    Shared by ``HttpProxyHandler`` and ``ForwardingHandler`` so the full
    retry / IP-rotation path in TargetManager is used by both modes.
    """
    # --- Read body ---
    body: bytes | None = None
    content_length = headers.get("content-length")
    if content_length:
        try:
            length = int(content_length)
            if length > _MAX_BODY_SIZE:
                _write_error(writer, 413, "Request Entity Too Large")
                await writer.drain()
                return
            body = await reader.readexactly(length)
        except (ValueError, asyncio.IncompleteReadError):
            _write_error(writer, 400, "Bad Request")
            await writer.drain()
            return

    # --- Route to manager ---
    manager = _find_first_manager(managers, url)
    if manager is None:
        logger.warning("No target matched for %s %s", method, url)
        get_metrics().record_request("unmatched", "no_match", 0.0)
        _write_error(writer, 502, f"No target configured for URL: {url}")
        await writer.drain()
        return

    # --- Submit and await ---
    future: asyncio.Future[ProxyResponse] = asyncio.get_running_loop().create_future()
    pending = PendingRequest(
        method=method,
        url=url,
        headers=dict(headers),
        body=body,
        future=future,
        arrival_time=time.monotonic(),
        max_queue_wait=manager._config.max_queue_wait,
        num_retries=manager._config.num_retries,
    )
    await manager.submit(pending)

    try:
        response: ProxyResponse = await asyncio.wait_for(
            future, timeout=manager._config.max_queue_wait + 5
        )
    except (asyncio.TimeoutError, TimeoutError):
        logger.warning(
            "ProxyServer: %s %s — gateway timeout waiting for response (queue_wait=%.1fs)",
            method, url, manager._config.max_queue_wait,
        )
        _write_error(writer, 504, "Gateway Timeout")
        await writer.drain()
        return
    except Exception as exc:
        logger.warning(
            "ProxyServer: %s %s — unexpected error: %s",
            method, url, exc,
        )
        _write_error(writer, 502, f"Bad Gateway: {exc}")
        await writer.drain()
        return

    if response.status >= 400:
        # Extract the structured error detail from JSON body if present
        detail = ""
        ct = response.headers.get("Content-Type", "")
        if "json" in ct and response.body:
            try:
                import json as _json
                parsed = _json.loads(response.body)
                detail = f" — {parsed.get('error', '')} {parsed.get('detail', '')}".rstrip()
            except Exception:
                pass
        logger.warning(
            "ProxyServer: %s %s → %d%s",
            method, url, response.status, detail,
        )

    get_metrics().record_response(manager._config.name, response.status)

    _write_http_response(writer, response, http_version)
    await writer.drain()


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

class RequestHandler(ABC):
    """Abstract base for a single proxy interaction mode.

    Implementations are stateless — instantiated once at server startup and
    reused across all connections.  Any per-request state must be local to
    ``handle``.

    Registration and dispatch order are controlled by ``_build_handlers``.
    To add a new mode, subclass this, implement both methods, and register
    the class in ``_MODE_REGISTRY`` / ``_HANDLER_ORDER``.
    """

    @abstractmethod
    def can_handle(
        self,
        method: str,
        target: str,
        http_version: str,
        headers: dict[str, str],
    ) -> bool:
        """Return True if this handler should own this request.

        Called in registration order; the first True wins.
        Must be synchronous and free of I/O.
        """

    @abstractmethod
    async def handle(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        method: str,
        target: str,
        http_version: str,
        headers: dict[str, str],
    ) -> None:
        """Process the request end-to-end and write the response.

        The request line and headers have already been consumed from *reader*.
        Do not close *writer* — ``ProxyServer`` owns the connection lifetime.
        """


# ---------------------------------------------------------------------------
# Concrete handlers
# ---------------------------------------------------------------------------

class ConnectTunnelHandler(RequestHandler):
    """HTTPS CONNECT tunnel mode.

    Establishes a raw TCP tunnel through an upstream proxy IP and relays
    bytes blindly in both directions.  Retries only apply to tunnel
    *establishment* — once the tunnel is up, mid-flight failures cannot be
    retried because the client has already committed TLS state.
    """

    def __init__(self, managers: list[TargetManager]) -> None:
        self._managers = managers

    def can_handle(self, method, target, http_version, headers) -> bool:
        return method.upper() == "CONNECT"

    async def handle(self, reader, writer, method, target, http_version, headers) -> None:
        logger.trace(  # type: ignore[attr-defined]
            "ConnectTunnelHandler: CONNECT %s", target
        )
        manager = _find_first_manager(self._managers, target)
        if manager is None:
            logger.warning("ConnectTunnelHandler: no target matched for CONNECT %s", target)
            _write_raw(writer, f"{http_version} 502 No target configured\r\n\r\n".encode())
            await writer.drain()
            return

        tunnel_writer: asyncio.StreamWriter | None = None
        tunnel_reader: asyncio.StreamReader | None = None
        active_address: str | None = None

        for _ in range(manager._config.num_retries + 1):
            address = await manager._pool.acquire(manager._config.max_queue_wait)
            if address is None:
                _write_raw(writer, f"{http_version} 504 Gateway Timeout\r\n\r\n".encode())
                await writer.drain()
                return

            host, _, port_str = address.rpartition(":")
            try:
                tunnel_reader, tunnel_writer = await asyncio.open_connection(
                    host, int(port_str)
                )
                tunnel_writer.write(
                    _build_connect_request(
                        target,
                        manager._config.proxy_username,
                        manager._config.proxy_password,
                    )
                )
                await tunnel_writer.drain()

                status_line = await asyncio.wait_for(tunnel_reader.readline(), timeout=10.0)
                # Drain and capture response headers for richer failure logging
                response_headers: list[bytes] = []
                while True:
                    line = await asyncio.wait_for(tunnel_reader.readline(), timeout=5.0)
                    if line in (b"\r\n", b"\n", b""):
                        break
                    response_headers.append(line.rstrip(b"\r\n"))

                if b"200" not in status_line:
                    detail = "; ".join(
                        h.decode("latin-1", errors="replace") for h in response_headers
                    )
                    raise ConnectionError(
                        f"External proxy rejected CONNECT: {status_line!r}"
                        + (f" ({detail})" if detail else "")
                    )

                logger.debug(
                    "ConnectTunnelHandler: CONNECT %s — tunnel established via %s",
                    target, address,
                )
                active_address = address
                break

            except Exception as exc:
                logger.warning(
                    "ConnectTunnelHandler: CONNECT %s via %s failed: %s",
                    target, address, exc,
                )
                if tunnel_writer:
                    try:
                        tunnel_writer.close()
                    except Exception:
                        pass
                    tunnel_writer = None
                await manager._pool.record_failure(address)
        else:
            _write_raw(writer, f"{http_version} 502 All proxies failed\r\n\r\n".encode())
            await writer.drain()
            return

        _write_raw(writer, b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()

        logger.trace(  # type: ignore[attr-defined]
            "ConnectTunnelHandler: CONNECT %s — relaying via %s", target, active_address
        )
        try:
            await _relay(reader, writer, tunnel_reader, tunnel_writer)
            logger.trace(  # type: ignore[attr-defined]
                "ConnectTunnelHandler: CONNECT %s — relay complete", target
            )
        finally:
            if tunnel_writer:
                try:
                    tunnel_writer.close()
                    await tunnel_writer.wait_closed()
                except Exception:
                    pass
            if active_address is not None:
                await manager._pool.record_success(active_address)


class HttpProxyHandler(RequestHandler):
    """Traditional HTTP proxy mode.

    Handles absolute-form requests (``GET http://example.com/ HTTP/1.1``).
    The full request is forwarded through an upstream proxy IP via aiohttp,
    giving TargetManager full visibility for retry and IP rotation.
    """

    def __init__(self, managers: list[TargetManager]) -> None:
        self._managers = managers

    def can_handle(self, method, target, http_version, headers) -> bool:
        return target.startswith("http://") or target.startswith("https://")

    async def handle(self, reader, writer, method, target, http_version, headers) -> None:
        logger.trace(  # type: ignore[attr-defined]
            "HttpProxyHandler: %s %s", method, target
        )
        await _submit_and_respond(
            reader, writer, method, target, http_version, headers, self._managers
        )


_FORWARDING_HEADER = "x-proxy-hopper-target"


class ForwardingHandler(RequestHandler):
    """Header-based forwarding mode.

    Clients set the ``X-Proxy-Hopper-Target`` header to the scheme and host
    of the real destination, then send the request to proxy-hopper as if it
    were the target server::

        GET /v1/data?foo=bar HTTP/1.1
        Host: proxy-hopper:8080
        X-Proxy-Hopper-Target: https://api.example.com

    Proxy-hopper strips the header, reconstructs
    ``https://api.example.com/v1/data?foo=bar``, rewrites ``Host``, and
    routes through ``_submit_and_respond`` — giving the same full retry /
    IP-rotation behaviour as HTTP proxy mode.

    Integration example (requests)::

        session = requests.Session()
        session.headers["X-Proxy-Hopper-Target"] = "https://api.example.com"
        # All normal URL building / urljoin works — nothing strips the header
        resp = session.get("http://proxy-hopper:8080/v1/endpoint")

    The header value may include a path prefix
    (``https://api.example.com/base``) which is prepended to the request path.
    """

    def __init__(self, managers: list[TargetManager]) -> None:
        self._managers = managers

    def can_handle(self, method, target, http_version, headers) -> bool:
        return _FORWARDING_HEADER in headers

    async def handle(self, reader, writer, method, target, http_version, headers) -> None:
        proxy_target = headers[_FORWARDING_HEADER].rstrip("/")
        real_url = proxy_target + target   # "https://api.example.com" + "/v1/data?foo=bar"

        # Rewrite Host to the target host and strip the forwarding header
        # so the upstream server never sees it.
        parsed = urlparse(real_url)
        rewritten_headers = {
            k: v for k, v in headers.items() if k != _FORWARDING_HEADER
        }
        rewritten_headers["host"] = parsed.netloc

        logger.trace(  # type: ignore[attr-defined]
            "ForwardingHandler: %s %s → %s", method, target, real_url
        )
        await _submit_and_respond(
            reader, writer, method, real_url, http_version, rewritten_headers, self._managers
        )


# ---------------------------------------------------------------------------
# Handler registry and factory
# ---------------------------------------------------------------------------

_MODE_REGISTRY: dict[str, type[RequestHandler]] = {
    "connect_tunnel": ConnectTunnelHandler,
    "http_proxy": HttpProxyHandler,
    "forwarding": ForwardingHandler,
}

# Dispatch priority order — evaluated top-to-bottom, first match wins.
# connect_tunnel must precede http_proxy (CONNECT never has an absolute URL).
# forwarding must precede http_proxy (paths like /https/... won't match
# the absolute-URL check, but ordering makes intent explicit).
_HANDLER_ORDER = ["connect_tunnel", "forwarding", "http_proxy"]

VALID_MODES: frozenset[str] = frozenset(_MODE_REGISTRY)


def _build_handlers(
    managers: list[TargetManager],
    enabled_modes: set[str],
) -> list[RequestHandler]:
    """Instantiate enabled handlers in dispatch-priority order."""
    return [
        _MODE_REGISTRY[name](managers)
        for name in _HANDLER_ORDER
        if name in enabled_modes
    ]
