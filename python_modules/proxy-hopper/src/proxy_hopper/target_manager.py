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
    def __init__(self, config: TargetConfig, backend: "IPPoolBackend") -> None:
        self._config = config
        self._pool = IPPool(config, backend)
        self._regex = config.compiled_regex()
        self._request_queue: asyncio.Queue[PendingRequest] = asyncio.Queue()
        self._tasks: list[asyncio.Task] = []
        self._running = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
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

    async def stop(self) -> None:
        self._running = False
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self._pool.stop()
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
                    request.future.set_exception(TimeoutError("Request expired in queue"))
                self._request_queue.task_done()
                continue

            address = await self._pool.acquire(request.time_remaining())
            if address is None:
                logger.warning(
                    "TargetManager '%s': no IP available for %s %s within %.2fs — dropping",
                    self._config.name, request.method, request.url, request.time_remaining(),
                )
                if not request.future.done():
                    request.future.set_exception(
                        TimeoutError("No IP available within the allowed wait time")
                    )
                self._request_queue.task_done()
                continue

            logger.debug(
                "TargetManager '%s': dispatching %s %s via %s",
                self._config.name, request.method, request.url, address,
            )
            asyncio.create_task(
                self._execute_request(address, request),
                name=f"ph:execute:{self._config.name}",
            )
            self._request_queue.task_done()

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

        try:
            logger.trace(  # type: ignore[attr-defined]
                "TargetManager '%s': opening connection to proxy %s for %s %s",
                self._config.name, address, request.method, request.url,
            )
            async with aiohttp.ClientSession() as session:
                async with session.request(
                    method=request.method,
                    url=request.url,
                    headers=forward_headers,
                    data=request.body,
                    proxy=proxy_url,
                    allow_redirects=False,
                    timeout=aiohttp.ClientTimeout(total=max(30.0, request.time_remaining())),
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
                        await self._pool.record_failure(address)
                        if request.can_retry():
                            retry = request.clone_for_retry()
                            logger.debug(
                                "TargetManager '%s': retrying %s %s",
                                self._config.name, request.method, request.url,
                            )
                            await self._request_queue.put(retry)
                        elif not request.future.done():
                            request.future.set_result(
                                ProxyResponse(resp.status, dict(resp.headers), body)
                            )
                    else:
                        outcome = "success"
                        logger.debug(
                            "TargetManager '%s': %s %s via %s → %d (%.3fs)",
                            self._config.name, request.method, request.url,
                            address, resp.status, elapsed,
                        )
                        await self._pool.record_success(address)
                        if not request.future.done():
                            request.future.set_result(
                                ProxyResponse(resp.status, dict(resp.headers), body)
                            )

        except aiohttp.ClientError as exc:
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
                request.future.set_exception(exc)

        except Exception as exc:  # pragma: no cover
            outcome = "error"
            logger.exception(
                "TargetManager '%s': unexpected error via %s for %s %s",
                self._config.name, address, request.method, request.url,
            )
            await self._pool.record_failure(address)
            if not request.future.done():
                request.future.set_exception(exc)

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
