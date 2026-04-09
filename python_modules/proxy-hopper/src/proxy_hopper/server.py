"""Raw asyncio TCP proxy server.

Handles both HTTP proxy requests (GET http://... HTTP/1.1) and HTTPS
tunnelling via the CONNECT method.  Dispatches matched requests to the
appropriate TargetManager; returns HTTP errors for unmatched or timed-out
requests.

HTTPS / CONNECT note
--------------------
When a client sends CONNECT hostname:443 the server can only see the
hostname and port — *not* the request path — because the subsequent traffic
is TLS-encrypted.  Regex matching for CONNECT requests therefore operates
on the ``host:port`` string only.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from .metrics import get_metrics
from .models import PendingRequest, ProxyResponse

if TYPE_CHECKING:
    from .target_manager import TargetManager

logger = logging.getLogger(__name__)

_MAX_HEADER_SIZE = 65_536   # 64 KiB
_MAX_BODY_SIZE = 10 * 1024 * 1024  # 10 MiB

_HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "proxy-connection", "te", "trailers", "transfer-encoding", "upgrade",
})


class ProxyServer:
    """Asyncio TCP server implementing the HTTP proxy protocol."""

    def __init__(
        self,
        target_managers: list["TargetManager"],
        host: str = "0.0.0.0",
        port: int = 8080,
    ) -> None:
        self._managers = target_managers
        self._host = host
        self._port = port
        self._server: asyncio.Server | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        for mgr in self._managers:
            await mgr.start()
        self._server = await asyncio.start_server(
            self._handle_client,
            host=self._host,
            port=self._port,
        )
        logger.info("Proxy server listening on %s:%d", self._host, self._port)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        for mgr in self._managers:
            await mgr.stop()

    async def serve_forever(self) -> None:
        async with self._server:
            await self._server.serve_forever()

    # ------------------------------------------------------------------
    # Connection handler
    # ------------------------------------------------------------------

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        peer = writer.get_extra_info("peername", "<unknown>")
        logger.debug("ProxyServer: new connection from %s", peer)
        try:
            await self._dispatch(reader, writer)
        except (ConnectionResetError, asyncio.IncompleteReadError, BrokenPipeError):
            logger.trace(  # type: ignore[attr-defined]
                "ProxyServer: connection from %s closed abruptly", peer
            )
        except Exception:
            logger.exception("ProxyServer: unhandled error for client %s", peer)
        finally:
            logger.trace(  # type: ignore[attr-defined]
                "ProxyServer: closing connection from %s", peer
            )
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _dispatch(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        method, target, http_version, headers = await _read_request_head(reader)
        if method.upper() == "CONNECT":
            await self._handle_connect(reader, writer, target, http_version)
        else:
            await self._handle_http(reader, writer, method, target, http_version, headers)

    # ------------------------------------------------------------------
    # HTTP proxy request
    # ------------------------------------------------------------------

    async def _handle_http(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        method: str,
        url: str,
        http_version: str,
        headers: dict[str, str],
    ) -> None:
        body: bytes | None = None
        content_length = headers.get("content-length")
        if content_length:
            try:
                length = int(content_length)
                if length > _MAX_BODY_SIZE:
                    _write_error(writer, 413, "Request Entity Too Large")
                    return
                body = await reader.readexactly(length)
            except (ValueError, asyncio.IncompleteReadError):
                _write_error(writer, 400, "Bad Request")
                return

        logger.trace(  # type: ignore[attr-defined]
            "ProxyServer: HTTP %s %s", method, url
        )
        manager = self._find_manager(url)
        if manager is None:
            logger.warning("ProxyServer: no target matched for %s %s", method, url)
            get_metrics().record_request("unmatched", "no_match", 0.0)
            _write_error(writer, 502, f"No target configured for URL: {url}")
            return

        future: asyncio.Future[ProxyResponse] = asyncio.get_event_loop().create_future()
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
            _write_error(writer, 504, "Gateway Timeout")
            return
        except Exception as exc:
            _write_error(writer, 502, f"Bad Gateway: {exc}")
            return

        _write_http_response(writer, response, http_version)
        await writer.drain()

    # ------------------------------------------------------------------
    # HTTPS CONNECT tunnelling
    # ------------------------------------------------------------------

    async def _handle_connect(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        target: str,
        http_version: str,
    ) -> None:
        logger.trace(  # type: ignore[attr-defined]
            "ProxyServer: CONNECT %s", target
        )
        manager = self._find_manager(target)
        if manager is None:
            logger.warning("ProxyServer: no target matched for CONNECT %s", target)
            _write_raw(writer, f"{http_version} 502 No target configured\r\n\r\n".encode())
            return

        tunnel_writer: asyncio.StreamWriter | None = None
        tunnel_reader: asyncio.StreamReader | None = None
        active_address: str | None = None

        for _ in range(manager._config.num_retries + 1):
            address = await manager._pool.acquire(manager._config.max_queue_wait)
            if address is None:
                _write_raw(writer, f"{http_version} 504 Gateway Timeout\r\n\r\n".encode())
                return

            host, _, port_str = address.rpartition(":")
            try:
                tunnel_reader, tunnel_writer = await asyncio.open_connection(
                    host, int(port_str)
                )
                tunnel_writer.write(
                    f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode()
                )
                await tunnel_writer.drain()

                status_line = await asyncio.wait_for(tunnel_reader.readline(), timeout=10.0)
                if b"200" not in status_line:
                    raise ConnectionError(
                        f"External proxy rejected CONNECT: {status_line!r}"
                    )
                # Drain remaining proxy response headers
                while True:
                    line = await asyncio.wait_for(tunnel_reader.readline(), timeout=5.0)
                    if line in (b"\r\n", b"\n", b""):
                        break
                logger.debug(
                    "ProxyServer: CONNECT %s — tunnel established via %s",
                    target, address,
                )
                active_address = address
                break  # tunnel established

            except Exception as exc:
                logger.warning("ProxyServer: CONNECT %s via %s failed: %s", target, address, exc)
                if tunnel_writer:
                    try:
                        tunnel_writer.close()
                    except Exception:
                        pass
                    tunnel_writer = None
                await manager._pool.record_failure(address)
        else:
            _write_raw(writer, f"{http_version} 502 All proxies failed\r\n\r\n".encode())
            return

        _write_raw(writer, b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()

        logger.trace(  # type: ignore[attr-defined]
            "ProxyServer: CONNECT %s — relaying data via %s", target, active_address
        )
        try:
            await _relay(reader, writer, tunnel_reader, tunnel_writer)
            logger.trace(  # type: ignore[attr-defined]
                "ProxyServer: CONNECT %s — relay complete", target
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

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _find_manager(self, url: str) -> "TargetManager | None":
        for mgr in self._managers:
            if mgr.matches(url):
                return mgr
        return None


# ---------------------------------------------------------------------------
# Low-level I/O helpers
# ---------------------------------------------------------------------------

async def _read_request_head(
    reader: asyncio.StreamReader,
) -> tuple[str, str, str, dict[str, str]]:
    raw_line = await reader.readline()
    if not raw_line:
        raise asyncio.IncompleteReadError(b"", None)

    request_line = raw_line.decode("latin-1").rstrip("\r\n")
    parts = request_line.split(" ", 2)
    if len(parts) != 3:
        raise ValueError(f"Malformed request line: {request_line!r}")
    method, target, version = parts

    headers: dict[str, str] = {}
    total = len(raw_line)
    while True:
        line = await reader.readline()
        total += len(line)
        if total > _MAX_HEADER_SIZE:
            raise ValueError("Request headers too large")
        if line in (b"\r\n", b"\n", b""):
            break
        decoded = line.decode("latin-1").rstrip("\r\n")
        if ":" in decoded:
            name, _, value = decoded.partition(":")
            headers[name.strip().lower()] = value.strip()

    return method, target, version, headers


def _write_error(writer: asyncio.StreamWriter, status: int, message: str) -> None:
    body = message.encode()
    writer.write(
        (
            f"HTTP/1.1 {status} {message}\r\n"
            f"Content-Type: text/plain\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Connection: close\r\n"
            f"\r\n"
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
