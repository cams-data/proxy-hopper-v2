"""Raw asyncio TCP server — request parsing and handler dispatch.

``ProxyServer`` owns the TCP listener and connection lifecycle.  Once the
request line and headers are parsed, ``_dispatch`` delegates to the first
``RequestHandler`` whose ``can_handle`` returns True.

All interaction-mode logic lives in ``handlers.py``.  To add a new mode,
see the docstring at the top of that module.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from .handlers import (
    RequestHandler,    # noqa: F401 — re-exported; useful for type hints in tests
    _build_handlers,
    _find_first_manager,
    _reason,            # noqa: F401 — re-exported; imported directly by tests
    _write_error,
)
from .metrics import get_metrics

if TYPE_CHECKING:
    from .target_manager import TargetManager

logger = logging.getLogger(__name__)

_MAX_HEADER_SIZE = 65_536   # 64 KiB

# Paths that are recognised as infrastructure health/readiness checks.
# Connections matching these are silently handled without connection-level logs.
_HEALTHCHECK_PATHS = frozenset({"/", "/health", "/healthz", "/ready", "/readyz", "/ping", "/status"})


def _is_healthcheck(peer: object, method: str, target: str) -> bool:
    """Return True if this looks like an infrastructure health/readiness probe.

    Matches loopback connections (127.x or ::1) making a GET or HEAD request
    to a well-known health path.  These are silenced at connection-log level
    to keep the log stream focused on real proxy traffic.
    """
    if not isinstance(peer, tuple) or not peer:
        return False
    host = peer[0]
    if host not in ("127.0.0.1", "::1") and not host.startswith("127."):
        return False
    if method.upper() not in ("GET", "HEAD"):
        return False
    # target may be an absolute-form URL or a path
    path = target.split("?", 1)[0]  # strip query string
    if "://" in path:
        from urllib.parse import urlparse
        path = urlparse(path).path or "/"
    return path in _HEALTHCHECK_PATHS


class ProxyServer:
    """Asyncio TCP server — parses requests and dispatches to handlers."""

    def __init__(
        self,
        target_managers: list[TargetManager],
        host: str = "0.0.0.0",
        port: int = 8080,
        enabled_modes: set[str] | None = None,
    ) -> None:
        from .handlers import VALID_MODES
        self._managers = target_managers
        self._host = host
        self._port = port
        self._server: asyncio.Server | None = None
        self._handlers: list[RequestHandler] = _build_handlers(
            target_managers,
            enabled_modes if enabled_modes is not None else set(VALID_MODES),
        )

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
        mode_names = [type(h).__name__ for h in self._handlers]
        logger.info(
            "Proxy server listening on %s:%d (modes: %s)",
            self._host, self._port, ", ".join(mode_names),
        )

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
        get_metrics().inc_active_connections()
        try:
            while True:
                keep_alive = await self._dispatch(reader, writer, peer)
                if not keep_alive:
                    break
        except (ConnectionResetError, asyncio.IncompleteReadError, BrokenPipeError):
            logger.trace(  # type: ignore[attr-defined]
                "ProxyServer: connection from %s closed abruptly", peer
            )
        except Exception:
            logger.exception("ProxyServer: unhandled error for client %s", peer)
        finally:
            get_metrics().dec_active_connections()
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _dispatch(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        peer: object,
    ) -> bool:
        """Handle one request.  Returns True if the connection should be kept alive."""
        method, target, http_version, headers = await _read_request_head(reader)

        # Honour explicit close requests; HTTP/1.0 is close-by-default.
        # HTTP/1.1 with no Connection header is keep-alive by default.
        client_wants_close = (
            headers.get("connection", "").lower() == "close"
            or http_version == "HTTP/1.0"
        )

        if not _is_healthcheck(peer, method, target):
            logger.debug("ProxyServer: %s %s from %s", method, target, peer)

        for handler in self._handlers:
            if handler.can_handle(method, target, http_version, headers):
                await handler.handle(reader, writer, method, target, http_version, headers)
                return not client_wants_close

        logger.warning(
            "ProxyServer: no handler claimed %s %s — enabled modes: %s",
            method, target, [type(h).__name__ for h in self._handlers],
        )
        _write_error(writer, 400, "Request format not supported by any enabled mode")
        await writer.drain()
        return False

    # ------------------------------------------------------------------
    # Backward-compatible shim (used in tests)
    # ------------------------------------------------------------------

    def _find_manager(self, url: str) -> TargetManager | None:
        return _find_first_manager(self._managers, url)


# ---------------------------------------------------------------------------
# Request head parser — kept here as it is server-level infrastructure
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
