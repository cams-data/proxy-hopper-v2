"""Per-target request queue management and dispatcher.

TargetManager owns:
  - The local asyncio request queue (tied to live TCP connections / Futures)
  - The dispatcher coroutine that pairs requests with available identities
  - HTTP forwarding logic (aiohttp) and retry decisions

All IP state and policy is delegated to IdentityQueue.  TargetManager never
imports or touches IPPoolStore or Backend directly.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING

import aiohttp

from .config import ProxyProvider, TargetConfig
from .identity.identity import Identity
from .logging_config import get_logger
from .metrics import get_metrics
from .models import HOP_BY_HOP_HEADERS, PendingRequest, ProxyResponse
from .pool import IdentityQueue

if TYPE_CHECKING:
    from .pool_store import IPPoolStore

logger = get_logger(__name__)

_RETRIABLE_STATUSES = frozenset({429, 502, 503, 504})
_HOP_BY_HOP = HOP_BY_HOP_HEADERS


class TargetManager:
    def __init__(
        self,
        config: TargetConfig,
        backend: "IPPoolStore",
        providers: list[ProxyProvider] | None = None,
        proxy_read_timeout: float | None = None,
        debug_quarantine: bool = False,
        quarantine_sweep_interval: float | None = None,
    ) -> None:
        self._config = config
        self._regex = config.compiled_regex()

        # Build auth map: address → aiohttp.BasicAuth (from provider credentials)
        provider_map = {p.name: p for p in (providers or [])}
        self._auth_map: dict[str, aiohttp.BasicAuth | None] = {}
        for ip in config.resolved_ips:
            if ip.provider and ip.provider in provider_map:
                p = provider_map[ip.provider]
                if p.auth is not None:
                    self._auth_map[ip.address] = aiohttp.BasicAuth(
                        p.auth.username, p.auth.password
                    )
                else:
                    self._auth_map[ip.address] = None
            else:
                self._auth_map[ip.address] = None

        queue_kwargs: dict = {"debug": debug_quarantine}
        if quarantine_sweep_interval is not None:
            queue_kwargs["sweep_interval"] = quarantine_sweep_interval
        self._queue = IdentityQueue(config, backend, **queue_kwargs)

        self._request_queue: asyncio.Queue[PendingRequest] = asyncio.Queue()
        self._tasks: list[asyncio.Task] = []
        self._inflight: set[asyncio.Task] = set()
        self._running = False
        self._session: aiohttp.ClientSession | None = None
        self._proxy_read_timeout = proxy_read_timeout

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        connector = aiohttp.TCPConnector(
            limit=0,               # no global cap — pool size controls concurrency
            keepalive_timeout=60,  # keep tunnels alive for 60s between requests
            enable_cleanup_closed=True,  # discard connections closed by proxy without FIN
        )
        self._session = aiohttp.ClientSession(
            auto_decompress=False,
            connector=connector,
            # Disable aiohttp's built-in cookie jar — the identity system
            # manages cookies per-(IP, target) in the backend.  Without this,
            # aiohttp would accumulate cookies in a shared jar across all IPs,
            # defeating per-identity isolation.
            cookie_jar=aiohttp.DummyCookieJar(),
        )
        await self._queue.start()
        self._running = True
        self._tasks = [
            asyncio.create_task(
                self._dispatcher_worker(),
                name=f"ph:dispatcher:{self._config.name}",
            ),
            asyncio.create_task(
                self._metrics_updater(),
                name=f"ph:metrics:{self._config.name}",
            ),
        ]
        logger.info("TargetManager '%s' started", self._config.name)

    async def stop(self, drain_timeout: float = 30.0) -> None:
        self._running = False

        # Stop accepting new work
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

        # Drain in-flight requests — give them a chance to complete cleanly
        if self._inflight:
            logger.info(
                "TargetManager '%s': waiting for %d in-flight request(s) to complete (timeout=%.0fs)",
                self._config.name, len(self._inflight), drain_timeout,
            )
            await asyncio.wait(set(self._inflight), timeout=drain_timeout)
            if self._inflight:
                logger.warning(
                    "TargetManager '%s': %d in-flight request(s) did not complete within %.0fs — cancelling",
                    self._config.name, len(self._inflight), drain_timeout,
                )
                for task in list(self._inflight):
                    task.cancel()
                await asyncio.gather(*list(self._inflight), return_exceptions=True)

        # Reject any requests still sitting in the queue
        rejected = 0
        while not self._request_queue.empty():
            try:
                pending = self._request_queue.get_nowait()
                if not pending.future.done():
                    pending.future.set_result(
                        self._error_response(503, "server_shutdown", pending, "Server is shutting down")
                    )
                rejected += 1
            except asyncio.QueueEmpty:
                break
        if rejected:
            logger.warning(
                "TargetManager '%s': rejected %d queued request(s) due to shutdown",
                self._config.name, rejected,
            )

        await self._queue.stop()
        if self._session is not None:
            await self._session.close()
            self._session = None
        logger.info("TargetManager '%s' stopped", self._config.name)

    # ------------------------------------------------------------------
    # Dynamic IP management (called by ProxyServer on target:update events)
    # ------------------------------------------------------------------

    async def add_address(
        self, address: str, provider: str = "", region_tag: str = ""
    ) -> None:
        """Add a new proxy IP — create its identity and push UUID to the queue."""
        self._auth_map[address] = None  # updated when manager is rebuilt from full config
        await self._queue.add_address(address, provider=provider, region_tag=region_tag)

    async def retire_address(self, address: str) -> None:
        """Mark a proxy IP as retired — its identity is discarded on next pop."""
        await self._queue.retire_address(address)

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def matches(self, url: str) -> bool:
        return bool(self._regex.search(url))

    async def submit(self, request: PendingRequest) -> None:
        await self._request_queue.put(request)
        depth = self._request_queue.qsize()
        logger.trace(
            "TargetManager '%s': enqueued %s %s (queue depth: %d)",
            self._config.name, request.method, request.url, depth,
        )
        get_metrics().set_queue_depth(self._config.name, depth)

    # ------------------------------------------------------------------
    # Dispatcher worker
    # ------------------------------------------------------------------

    async def _dispatcher_worker(self) -> None:
        while self._running:
            try:
                request = await asyncio.wait_for(
                    self._request_queue.get(), timeout=1.0
                )
            except (asyncio.TimeoutError, TimeoutError):
                continue

            logger.trace(
                "TargetManager '%s': dequeued %s %s",
                self._config.name, request.method, request.url,
            )

            if request.is_expired():
                logger.warning(
                    "TargetManager '%s': %s %s expired in queue — dropping",
                    self._config.name, request.method, request.url,
                )
                get_metrics().record_queue_expired(self._config.name)
                if not request.future.done():
                    request.future.set_result(
                        self._error_response(503, "queue_timeout", request, "Request expired waiting in queue")
                    )
                self._request_queue.task_done()
                continue

            queue_wait = time.monotonic() - request.arrival_time
            result = await self._queue.acquire(request.time_remaining())
            if result is None:
                logger.warning(
                    "TargetManager '%s': no identity available for %s %s within %.2fs — dropping",
                    self._config.name, request.method, request.url, request.time_remaining(),
                )
                get_metrics().record_queue_expired(self._config.name)
                if not request.future.done():
                    request.future.set_result(
                        self._error_response(503, "no_ip_available", request, "No proxy IP available within the allowed wait time")
                    )
                self._request_queue.task_done()
                continue

            uuid, identity = result
            get_metrics().record_queue_wait(self._config.name, queue_wait)

            logger.debug(
                "TargetManager '%s': dispatching %s %s via %s",
                self._config.name, request.method, request.url, identity.address,
            )
            task = asyncio.create_task(
                self._execute_request(uuid, identity, request),
                name=f"ph:execute:{self._config.name}",
            )
            self._inflight.add(task)
            task.add_done_callback(self._inflight.discard)
            self._request_queue.task_done()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _error_response(status: int, reason: str, request: "PendingRequest", detail: str = "") -> "ProxyResponse":
        body = json.dumps({
            "error": reason,
            "detail": detail,
            "url": request.url,
            "method": request.method,
            "retries_attempted": request.failure_count,
            "retries_allowed": request.num_retries,
        }, indent=2).encode()
        return ProxyResponse(status, {"Content-Type": "application/json"}, body)

    # ------------------------------------------------------------------
    # Request execution — top-level entry point
    # ------------------------------------------------------------------

    async def _execute_request(
        self, uuid: str, identity: Identity, request: PendingRequest
    ) -> None:
        forward_headers = self._build_request_headers(request, identity)
        proxy_url = f"http://{identity.address}"
        proxy_auth = self._auth_map.get(identity.address)
        start = time.monotonic()
        outcome = "unknown"

        try:
            logger.trace(
                "TargetManager '%s': opening connection to proxy %s for %s %s",
                self._config.name, identity.address, request.method, request.url,
            )
            async with self._session.request(  # type: ignore[union-attr]
                method=request.method,
                url=request.url,
                headers=forward_headers,
                data=request.body,
                proxy=proxy_url,
                proxy_auth=proxy_auth,
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(
                    total=max(30.0, request.time_remaining()),
                    sock_read=self._proxy_read_timeout,
                ),
            ) as resp:
                body = await resp.read()
                elapsed = time.monotonic() - start

                if resp.status in _RETRIABLE_STATUSES:
                    outcome = "rate_limited" if resp.status == 429 else "server_error"
                    await self._handle_retriable_response(uuid, identity, request, resp.status, elapsed)
                else:
                    outcome = "success"
                    await self._handle_success(uuid, identity, request, resp, body, elapsed)

        except (aiohttp.ClientError, asyncio.TimeoutError, TimeoutError) as exc:
            outcome = "connection_error"
            logger.warning(
                "TargetManager '%s': connection error via %s for %s %s: %s",
                self._config.name, identity.address, request.method, request.url, exc,
            )
            await self._handle_request_exception(
                uuid, identity, request, exc,
                status=502, reason="connection_error",
            )

        except Exception as exc:  # pragma: no cover
            outcome = "error"
            logger.exception(
                "TargetManager '%s': unexpected error via %s for %s %s",
                self._config.name, identity.address, request.method, request.url,
            )
            await self._handle_request_exception(
                uuid, identity, request, exc,
                status=502, reason="proxy_error",
            )

        finally:
            get_metrics().record_request(self._config.name, outcome, time.monotonic() - start, tag=request.tag)

    # ------------------------------------------------------------------
    # Request execution — decomposed helpers
    # ------------------------------------------------------------------

    def _build_request_headers(
        self, request: PendingRequest, identity: Identity
    ) -> dict[str, str]:
        """Strip hop-by-hop headers and apply identity fingerprint + cookies."""
        headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP_BY_HOP}
        return identity.apply_to_headers(headers)

    async def _retry_or_resolve_failure(
        self,
        request: PendingRequest,
        status: int,
        reason: str,
        detail: str,
    ) -> None:
        """Re-queue the request for retry or resolve the future with an error response."""
        if request.can_retry():
            get_metrics().record_retry(self._config.name)
            logger.debug(
                "TargetManager '%s': retrying %s %s",
                self._config.name, request.method, request.url,
            )
            await self._request_queue.put(request.clone_for_retry())
        elif not request.future.done():
            get_metrics().record_retry_exhaustion(self._config.name)
            logger.warning(
                "TargetManager '%s': %s %s — retries exhausted (%d/%d): %s",
                self._config.name, request.method, request.url,
                request.failure_count, request.num_retries, detail,
            )
            request.future.set_result(self._error_response(status, reason, request, detail))

    async def _handle_retriable_response(
        self,
        uuid: str,
        identity: Identity,
        request: PendingRequest,
        status: int,
        elapsed: float,
    ) -> None:
        """Handle a 429 / 502 / 503 / 504 response from upstream."""
        logger.warning(
            "TargetManager '%s': %s %s via %s → %d (%.3fs)",
            self._config.name, request.method, request.url, identity.address, status, elapsed,
        )
        will_rotate = status == 429 and self._config.identity.rotate_on_429
        was_quarantined = await self._queue.record_failure(
            uuid, identity, elapsed, return_uuid=not will_rotate
        )
        if not was_quarantined and will_rotate:
            # Not yet quarantined — rotate identity immediately on 429.
            # record_failure was called with return_uuid=False so the old UUID
            # was not scheduled for return; rotate disposes of it cleanly.
            await self._queue.rotate(uuid, identity, elapsed)
        await self._retry_or_resolve_failure(
            request, status, "upstream_error",
            f"Upstream returned {status} after exhausting retries",
        )

    async def _handle_success(
        self,
        uuid: str,
        identity: Identity,
        request: PendingRequest,
        resp: "aiohttp.ClientResponse",
        body: bytes,
        elapsed: float,
    ) -> None:
        """Handle a successful (non-retriable) upstream response."""
        logger.debug(
            "TargetManager '%s': %s %s via %s → %d (%.3fs)",
            self._config.name, request.method, request.url, identity.address, resp.status, elapsed,
        )
        identity.update_from_response(list(resp.raw_headers))
        identity.record_request()

        limit = self._config.identity.rotate_after_requests
        if limit is not None and identity.request_count >= limit:
            # Hit request limit — voluntary rotation; IP was healthy so reset failures.
            await self._queue.rotate(uuid, identity, elapsed, reset_failures=True)
        else:
            await self._queue.record_success(uuid, identity, elapsed)

        if not request.future.done():
            request.future.set_result(ProxyResponse(resp.status, dict(resp.headers), body))

    async def _handle_request_exception(
        self,
        uuid: str,
        identity: Identity,
        request: PendingRequest,
        exc: BaseException,
        *,
        status: int,
        reason: str,
    ) -> None:
        """Shared failure handler for connection errors and unexpected exceptions."""
        await self._queue.record_failure(uuid, identity, 0.0)
        await self._retry_or_resolve_failure(
            request, status, reason,
            f"{type(exc).__name__}: {exc}",
        )

    # ------------------------------------------------------------------
    # Metrics updater
    # ------------------------------------------------------------------

    async def _metrics_updater(self) -> None:
        while self._running:
            await asyncio.sleep(5)
            try:
                status = await self._queue.get_status()
                m = get_metrics()
                m.set_available_ips(self._config.name, status["available_ips"])
                m.set_quarantined_ips(self._config.name, len(status["quarantined_ips"]))
                m.set_queue_depth(self._config.name, self._request_queue.qsize())
            except Exception:
                logger.debug(
                    "TargetManager '%s': metrics update failed",
                    self._config.name, exc_info=True,
                )
