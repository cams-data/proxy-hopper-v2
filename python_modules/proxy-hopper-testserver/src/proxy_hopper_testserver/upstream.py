"""Controllable upstream HTTP server.

Simulates a third-party API with injectable failure modes. Used in
integration tests to drive proxy-hopper into specific states (quarantine,
retry exhaustion, etc.) without needing real external services.

Failure modes
-------------
normal          Respond 200 with a JSON body.
http_error      Respond with a fixed HTTP status code (e.g. 500, 429, 503).
hang            Accept the connection, read the request, then never respond.
                Useful for triggering sock_read timeouts.
close           Accept the connection then immediately close it without
                sending any response. Triggers ClientConnectionError /
                ServerDisconnectedError in aiohttp.
slow            Respond normally but after a configurable delay. Simulates
                geographically distant or overloaded servers.

All modes are applied globally — every request gets the same treatment
until the mode is changed.  Per-path overrides are intentionally out of
scope for now; add them when tests require finer-grained control.

Future extension points
-----------------------
- Per-path mode overrides: route-level dict keyed by path prefix
- Per-request counters: "serve 3 errors then go normal"
- Response body injection: return specific payloads for assertion
- Latency distribution: jitter around a base delay
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any

from aiohttp import web

logger = logging.getLogger(__name__)


class UpstreamMode(Enum):
    NORMAL = auto()
    HTTP_ERROR = auto()
    HANG = auto()
    CLOSE = auto()
    SLOW = auto()


@dataclass
class UpstreamState:
    mode: UpstreamMode = UpstreamMode.NORMAL
    # HTTP_ERROR mode
    error_status: int = 500
    # SLOW mode
    delay: float = 0.0
    # Counters — incremented on every request received
    request_count: int = 0
    # Per-mode request counts
    counts: dict[str, int] = field(default_factory=dict)


class UpstreamServer:
    """Lightweight aiohttp server whose behaviour is controlled at runtime.

    Usage::

        async with UpstreamServer() as server:
            url = server.url          # e.g. "http://127.0.0.1:PORT"
            server.set_mode("normal")
            # ... make requests ...
            server.set_mode("http_error", status=500)
            # ... assert quarantine state ...
    """

    def __init__(self) -> None:
        self._state = UpstreamState()
        self._app = web.Application()
        self._app.router.add_route("*", "/{path_info:.*}", self._handle)
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._port: int = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        self._runner = web.AppRunner(self._app, access_log=None)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await self._site.start()
        # Resolve the ephemeral port
        self._port = self._site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        logger.debug("UpstreamServer: listening on %s", self.url)

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        logger.debug("UpstreamServer: stopped")

    async def __aenter__(self) -> "UpstreamServer":
        await self.start()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.stop()

    # ------------------------------------------------------------------
    # Control interface
    # ------------------------------------------------------------------

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._port}"

    @property
    def request_count(self) -> int:
        return self._state.request_count

    def reset(self) -> None:
        """Reset to normal mode and zero all counters."""
        self._state = UpstreamState()

    def set_mode(self, mode: str, **kwargs: Any) -> None:
        """Switch the server's response behaviour.

        Parameters
        ----------
        mode:
            One of: ``normal``, ``http_error``, ``hang``, ``close``, ``slow``.
        status:
            HTTP status code (``http_error`` mode only). Default 500.
        delay:
            Seconds to wait before responding (``slow`` mode only). Default 1.0.
        """
        m = UpstreamMode[mode.upper()]
        self._state.mode = m
        if m == UpstreamMode.HTTP_ERROR:
            self._state.error_status = int(kwargs.get("status", 500))
        elif m == UpstreamMode.SLOW:
            self._state.delay = float(kwargs.get("delay", 1.0))
        logger.debug("UpstreamServer: mode → %s %s", mode, kwargs or "")

    # ------------------------------------------------------------------
    # Request handler
    # ------------------------------------------------------------------

    async def _handle(self, request: web.Request) -> web.Response:
        self._state.request_count += 1
        mode_key = self._state.mode.name
        self._state.counts[mode_key] = self._state.counts.get(mode_key, 0) + 1

        mode = self._state.mode

        if mode == UpstreamMode.CLOSE:
            # Drop the connection immediately — no response written
            raise web.HTTPInternalServerError()  # aiohttp closes after unhandled exc

        if mode == UpstreamMode.HANG:
            # Hold the connection open indefinitely — caller's sock_read will time out
            await asyncio.sleep(3600)
            return web.Response(status=200, text="never reached")

        if mode == UpstreamMode.SLOW:
            await asyncio.sleep(self._state.delay)

        if mode == UpstreamMode.HTTP_ERROR:
            body = json.dumps({
                "error": f"Simulated {self._state.error_status}",
                "request_count": self._state.request_count,
            })
            return web.Response(
                status=self._state.error_status,
                content_type="application/json",
                text=body,
            )

        # NORMAL (and SLOW after delay)
        body = json.dumps({
            "ok": True,
            "path": request.path,
            "request_count": self._state.request_count,
        })
        return web.Response(status=200, content_type="application/json", text=body)
