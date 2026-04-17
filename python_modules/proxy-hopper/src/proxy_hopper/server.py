"""Raw asyncio TCP server — request parsing and handler dispatch.

``ProxyServer`` owns the TCP listener and connection lifecycle.  Once the
request line and headers are parsed, ``_dispatch`` delegates to the first
``RequestHandler`` whose ``can_handle`` returns True.

All interaction-mode logic lives in ``handlers.py``.  To add a new mode,
see the docstring at the top of that module.

Hot-reload
----------
When a ``ProxyRepository`` is supplied at construction, ``ProxyServer``
subscribes to its change channel and adds/replaces/removes ``TargetManager``
instances at runtime without restarting the server.  The ``_managers`` list
is mutated in-place so that handler callbacks always see the current state.
Provider change events update ``_providers`` so newly built managers pick up
fresh credentials; IP changes are cascaded to target update events by the
repository itself.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from .handlers import (
    RequestHandler,    # noqa: F401 — re-exported; useful for type hints in tests
    _build_handlers,
    _find_first_manager,
    _reason,            # noqa: F401 — re-exported; imported directly by tests
    _write_error,
)
from .logging_config import get_logger
from .metrics import get_metrics

if TYPE_CHECKING:
    from .config import AuthConfig, ProxyProvider, TargetConfig
    from .pool_store import IPPoolStore
    from .repository import ProxyRepository
    from .target_manager import TargetManager

logger = get_logger(__name__)

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
        auth_config: "AuthConfig | None" = None,
        runtime_secret: str = "",
        pool_store: "IPPoolStore | None" = None,
        repository: "ProxyRepository | None" = None,
        providers: "list[ProxyProvider] | None" = None,
        proxy_read_timeout: float | None = None,
        debug_quarantine: bool = False,
        quarantine_sweep_interval: float | None = None,
    ) -> None:
        from .handlers import VALID_MODES
        self._managers = target_managers
        self._host = host
        self._port = port
        self._server: asyncio.Server | None = None
        self._pool_store = pool_store
        self._repository = repository
        self._providers = list(providers) if providers else []
        self._proxy_read_timeout = proxy_read_timeout
        self._debug_quarantine = debug_quarantine
        self._quarantine_sweep_interval = quarantine_sweep_interval
        self._change_listener_task: asyncio.Task | None = None
        self._handlers: list[RequestHandler] = _build_handlers(
            target_managers,
            enabled_modes if enabled_modes is not None else set(VALID_MODES),
            auth_config=auth_config,
            runtime_secret=runtime_secret,
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
        if self._repository is not None:
            self._change_listener_task = asyncio.create_task(
                self._config_change_listener(),
                name="ph:server:config-listener",
            )

    async def stop(self) -> None:
        if self._change_listener_task:
            self._change_listener_task.cancel()
            await asyncio.gather(self._change_listener_task, return_exceptions=True)
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        for mgr in self._managers:
            await mgr.stop()

    async def serve_forever(self) -> None:
        async with self._server:
            await self._server.serve_forever()

    # ------------------------------------------------------------------
    # Hot-reload — config change listener
    # ------------------------------------------------------------------

    async def _config_change_listener(self) -> None:
        """Subscribe to ProxyRepository changes and sync managers + providers.

        Automatically restarts on unexpected errors with exponential backoff
        so a transient backend blip does not permanently disable hot-reload.
        """
        assert self._repository is not None
        backoff = 1.0
        while True:
            try:
                async with self._repository.subscribe_changes() as events:
                    backoff = 1.0  # reset on successful subscription
                    async for event in events:
                        try:
                            await self._apply_change(event)
                        except Exception:
                            logger.exception(
                                "ProxyServer: error applying change event %s:%s/%s",
                                event.entity, event.type, event.name,
                            )
            except asyncio.CancelledError:
                return  # normal shutdown — do not restart
            except Exception:
                logger.exception(
                    "ProxyServer: config change listener crashed — restarting in %.0fs",
                    backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    async def _apply_change(self, event) -> None:
        if event.entity == "target":
            await self._apply_target_change(event)
        elif event.entity == "provider":
            self._apply_provider_change(event)

    async def _apply_target_change(self, event) -> None:
        if event.type == "add":
            config = await self._repository.get_target(event.name)
            if config is None:
                logger.warning("ProxyServer: add event for target '%s' but not found in repository", event.name)
                return
            mgr = self._build_manager(config)
            await mgr.start()
            self._managers.append(mgr)
            logger.info("ProxyServer: dynamically added target '%s'", event.name)

        elif event.type == "update":
            config = await self._repository.get_target(event.name)
            if config is None:
                logger.warning("ProxyServer: update event for target '%s' but not found in repository", event.name)
                return
            old = next((m for m in self._managers if m._config.name == event.name), None)

            # Diff IPs: create identities for new addresses and retire removed ones.
            # The pool's SETNX init-guard is already claimed, so the new manager's
            # start() won't re-seed — we handle the delta here via the old manager's
            # shared backend queue.
            if old is not None:
                old_addrs = {ip.address for ip in old._config.resolved_ips}
                new_addrs = {ip.address for ip in config.resolved_ips}
                addr_to_ip = {ip.address: ip for ip in config.resolved_ips}
                for addr in new_addrs - old_addrs:
                    resolved_ip = addr_to_ip[addr]
                    try:
                        await old.add_address(
                            addr,
                            provider=resolved_ip.provider,
                            region_tag=resolved_ip.region_tag,
                        )
                    except Exception:
                        logger.exception(
                            "ProxyServer: failed to add identity for new IP '%s' on target '%s'",
                            addr, config.name,
                        )
                for addr in old_addrs - new_addrs:
                    try:
                        await old.retire_address(addr)
                    except Exception:
                        logger.exception(
                            "ProxyServer: failed to retire IP '%s' on target '%s'",
                            addr, config.name,
                        )

            new_mgr = self._build_manager(config)
            await new_mgr.start()
            if old is not None:
                # In-place list mutation keeps handler references valid
                idx = self._managers.index(old)
                self._managers[idx] = new_mgr
                await old.stop()
            else:
                self._managers.append(new_mgr)
            logger.info("ProxyServer: dynamically updated target '%s'", event.name)

        elif event.type == "remove":
            old = next((m for m in self._managers if m._config.name == event.name), None)
            if old is None:
                return
            self._managers[:] = [m for m in self._managers if m is not old]
            await old.stop()
            logger.info("ProxyServer: dynamically removed target '%s'", event.name)

    def _apply_provider_change(self, event) -> None:
        """Keep self._providers in sync so new managers get fresh credentials."""
        from .repository import _dict_to_provider
        if event.type == "remove":
            self._providers[:] = [p for p in self._providers if p.name != event.name]
            logger.info("ProxyServer: provider '%s' removed from local cache", event.name)
        elif event.data is not None:
            new_p = _dict_to_provider(event.data)
            idx = next((i for i, p in enumerate(self._providers) if p.name == event.name), None)
            if idx is not None:
                self._providers[idx] = new_p
            else:
                self._providers.append(new_p)
            logger.info("ProxyServer: provider '%s' updated in local cache", event.name)

    def _build_manager(self, config: "TargetConfig") -> "TargetManager":
        from .target_manager import TargetManager
        kwargs: dict = {}
        if self._quarantine_sweep_interval is not None:
            kwargs["quarantine_sweep_interval"] = self._quarantine_sweep_interval
        return TargetManager(
            config=config,
            backend=self._pool_store,
            providers=self._providers,
            proxy_read_timeout=self._proxy_read_timeout,
            debug_quarantine=self._debug_quarantine,
            **kwargs,
        )

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
            logger.trace(
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
