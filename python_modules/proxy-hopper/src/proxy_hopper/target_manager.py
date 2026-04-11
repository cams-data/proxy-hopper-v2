"""Per-target request queue management and dispatcher.

TargetManager owns:
  - The local asyncio request queue (tied to live TCP connections / Futures)
  - The dispatcher coroutine that pairs requests with available IPs
  - HTTP forwarding logic (aiohttp) and retry decisions

All IP state and policy is delegated to IPPool.  TargetManager never
imports or touches IPPoolBackend directly.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING

import aiohttp

from .config import TargetConfig
from .metrics import get_metrics
from .models import PendingRequest, ProxyResponse
from .pool import IPPool

if TYPE_CHECKING:
    from .backend.base import IPPoolBackend

logger = logging.getLogger(__name__)

_RETRIABLE_STATUSES = frozenset({429, 502, 503, 504})
_HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "proxy-connection", "te", "trailers", "transfer-encoding", "upgrade",
})


class TargetManager:
    def __init__(
        self,
        config: TargetConfig,
        backend: "IPPoolBackend",
        proxy_read_timeout: float | None = None,
        debug_quarantine: bool = False,
        quarantine_sweep_interval: float | None = None,
    ) -> None:
        self._config = config
        pool_kwargs: dict = {"debug": debug_quarantine}
        if quarantine_sweep_interval is not None:
            pool_kwargs["sweep_interval"] = quarantine_sweep_interval
        self._pool = IPPool(config, backend, **pool_kwargs)
        self._regex = config.compiled_regex()
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
        )
        await self._pool.start()
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

        await self._pool.stop()
        if self._session is not None:
            await self._session.close()
            self._session = None
        logger.info("TargetManager '%s' stopped", self._config.name)

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def matches(self, url: str) -> bool:
        return bool(self._regex.search(url))

    async def submit(self, request: PendingRequest) -> None:
        await self._request_queue.put(request)
        depth = self._request_queue.qsize()
        logger.trace(  # type: ignore[attr-defined]
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

            logger.trace(  # type: ignore[attr-defined]
                "TargetManager '%s': dequeued %s %s",
                self._config.name, request.method, request.url,
            )

            if request.is_expired():
                logger.warning(
                    "TargetManager '%s': %s %s expired in queue — dropping",
                    self._config.name, request.method, request.url,
                )
                if not request.future.done():
                    request.future.set_result(
                        self._error_response(503, "queue_timeout", request, "Request expired waiting in queue")
                    )
                self._request_queue.task_done()
                continue

            address = await self._pool.acquire(request.time_remaining())
            if address is None:
                logger.warning(
                    "TargetManager '%s': no IP available for %s %s within %.2fs — dropping",
                    self._config.name, request.method, request.url, request.time_remaining(),
                )
                if not request.future.done():
                    request.future.set_result(
                        self._error_response(503, "no_ip_available", request, "No proxy IP available within the allowed wait time")
                    )
                self._request_queue.task_done()
                continue

            logger.debug(
                "TargetManager '%s': dispatching %s %s via %s",
                self._config.name, request.method, request.url, address,
            )
            task = asyncio.create_task(
                self._execute_request(address, request),
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
    # Request execution
    # ------------------------------------------------------------------

    async def _execute_request(self, address: str, request: PendingRequest) -> None:
        proxy_url = f"http://{address}"
        forward_headers = {
            k: v for k, v in request.headers.items()
            if k.lower() not in _HOP_BY_HOP
        }
        start = time.monotonic()
        outcome = "unknown"

        proxy_auth = (
            aiohttp.BasicAuth(
                self._config.proxy_username,
                self._config.proxy_password or "",
            )
            if self._config.proxy_username is not None
            else None
        )

        try:
            logger.trace(  # type: ignore[attr-defined]
                "TargetManager '%s': opening connection to proxy %s for %s %s",
                self._config.name, address, request.method, request.url,
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
                    logger.warning(
                        "TargetManager '%s': %s %s via %s → %d %s (%.3fs)",
                        self._config.name, request.method, request.url,
                        address, resp.status, outcome, elapsed,
                    )
                    await self._pool.record_failure(address, elapsed)
                    if request.can_retry():
                        retry = request.clone_for_retry()
                        logger.debug(
                            "TargetManager '%s': retrying %s %s",
                            self._config.name, request.method, request.url,
                        )
                        await self._request_queue.put(retry)
                    elif not request.future.done():
                        logger.warning(
                            "TargetManager '%s': %s %s — retries exhausted after %d/%d attempts, upstream returned %d",
                            self._config.name, request.method, request.url,
                            request.failure_count, request.num_retries, resp.status,
                        )
                        request.future.set_result(
                            self._error_response(
                                resp.status,
                                "upstream_error",
                                request,
                                f"Upstream returned {resp.status} after exhausting retries",
                            )
                        )
                else:
                    outcome = "success"
                    logger.debug(
                        "TargetManager '%s': %s %s via %s → %d (%.3fs)",
                        self._config.name, request.method, request.url,
                        address, resp.status, elapsed,
                    )
                    await self._pool.record_success(address, elapsed)
                    if not request.future.done():
                        request.future.set_result(
                            ProxyResponse(resp.status, dict(resp.headers), body)
                        )

        except (aiohttp.ClientError, asyncio.TimeoutError, TimeoutError) as exc:
            outcome = "connection_error"
            logger.warning(
                "TargetManager '%s': connection error via %s for %s %s: %s",
                self._config.name, address, request.method, request.url, exc,
            )
            await self._pool.record_failure(address)
            if request.can_retry():
                logger.debug(
                    "TargetManager '%s': scheduling retry for %s %s after connection error",
                    self._config.name, request.method, request.url,
                )
                await self._request_queue.put(request.clone_for_retry())
            elif not request.future.done():
                logger.warning(
                    "TargetManager '%s': %s %s — retries exhausted after %d/%d attempts, connection error: %s",
                    self._config.name, request.method, request.url,
                    request.failure_count, request.num_retries, exc,
                )
                request.future.set_result(
                    self._error_response(
                        502,
                        "connection_error",
                        request,
                        f"{type(exc).__name__}: {exc}",
                    )
                )

        except Exception as exc:  # pragma: no cover
            outcome = "error"
            logger.exception(
                "TargetManager '%s': unexpected error via %s for %s %s",
                self._config.name, address, request.method, request.url,
            )
            await self._pool.record_failure(address)
            if request.can_retry():
                logger.debug(
                    "TargetManager '%s': scheduling retry for %s %s after unexpected error",
                    self._config.name, request.method, request.url,
                )
                await self._request_queue.put(request.clone_for_retry())
            elif not request.future.done():
                logger.error(
                    "TargetManager '%s': %s %s — retries exhausted after %d/%d attempts, unexpected error: %s",
                    self._config.name, request.method, request.url,
                    request.failure_count, request.num_retries, exc,
                )
                request.future.set_result(
                    self._error_response(
                        502,
                        "proxy_error",
                        request,
                        f"{type(exc).__name__}: {exc}",
                    )
                )

        finally:
            get_metrics().record_request(self._config.name, outcome, time.monotonic() - start)

    # ------------------------------------------------------------------
    # Metrics updater
    # ------------------------------------------------------------------

    async def _metrics_updater(self) -> None:
        while self._running:
            await asyncio.sleep(5)
            try:
                status = await self._pool.get_status()
                m = get_metrics()
                m.set_available_ips(self._config.name, status["available_ips"])
                m.set_quarantined_ips(self._config.name, len(status["quarantined_ips"]))
                m.set_queue_depth(self._config.name, self._request_queue.qsize())
            except Exception:
                logger.debug(
                    "TargetManager '%s': metrics update failed",
                    self._config.name, exc_info=True,
                )
