"""Background IP health prober.

Periodically tests every known proxy IP address against a set of well-known
endpoints and records the results as Prometheus metrics.  Completely
independent of the pool, backend, and target machinery — it has no side
effects on IP state and no knowledge of targets.

The prober works from the deduplicated set of IP addresses extracted from the
config at startup.  Because the same IP often appears in multiple targets,
deduplication ensures each address is tested exactly once per interval.

Metrics written
---------------
  proxy_hopper_probe_success_total{address}                Counter
  proxy_hopper_probe_failure_total{address, reason}        Counter
  proxy_hopper_probe_duration_seconds{address}             Histogram
  proxy_hopper_ip_reachable{address}                       Gauge  (1=up, 0=down)
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import time
from typing import Sequence

import aiohttp

from .config import TargetConfig
from .metrics import get_metrics

_Auth = aiohttp.BasicAuth  # alias for brevity

logger = logging.getLogger(__name__)

# Default probe endpoints — plain HTTP so they work through HTTP-only proxies
# that do not support CONNECT tunnelling.  HTTPS probe URLs would require the
# proxy to establish a TLS tunnel, which plain HTTP proxies reject.
DEFAULT_PROBE_URLS: tuple[str, ...] = (
    "http://1.1.1.1",
    "http://www.google.com",
)


class IPProber:
    """Background task that health-checks proxy IPs and exports metrics.

    Parameters
    ----------
    targets:
        All configured targets.  IP addresses are deduplicated across targets.
    probe_urls:
        Endpoints to probe through each IP.  One URL is used per probe cycle,
        rotating round-robin, to keep outbound traffic light.
    interval:
        Seconds between probe runs for each IP.
    timeout:
        Per-probe HTTP timeout in seconds.
    """

    def __init__(
        self,
        targets: Sequence[TargetConfig],
        probe_urls: Sequence[str] = DEFAULT_PROBE_URLS,
        interval: float = 60.0,
        timeout: float = 10.0,
        debug: bool = False,
    ) -> None:
        # Deduplicate IPs across all targets — order is arbitrary but stable.
        # First target to claim an address wins for credentials.
        seen: set[str] = set()
        unique: list[str] = []
        auth_map: dict[str, _Auth | None] = {}
        for target in targets:
            auth = (
                _Auth(target.proxy_username, target.proxy_password or "")
                if target.proxy_username is not None
                else None
            )
            for host, port in target.resolved_ip_list():
                address = f"{host}:{port}"
                if address not in seen:
                    seen.add(address)
                    unique.append(address)
                    auth_map[address] = auth

        self._addresses = unique
        self._auth_map = auth_map
        self._probe_urls = list(probe_urls)
        self._interval = interval
        self._timeout = timeout
        self._debug = debug
        self._task: asyncio.Task | None = None
        self._session: aiohttp.ClientSession | None = None
        self._running = False
        # Rotate probe URLs independently per address
        self._url_cycles = {
            addr: itertools.cycle(self._probe_urls) for addr in self._addresses
        }

        logger.info(
            "IPProber: initialised with %d unique address(es), interval=%.0fs, urls=%s",
            len(self._addresses), self._interval, self._probe_urls,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if not self._addresses:
            logger.warning("IPProber: no addresses to probe — task not started")
            return
        if not self._probe_urls:
            logger.warning("IPProber: no probe URLs configured — task not started")
            return

        self._session = aiohttp.ClientSession()
        self._running = True
        self._task = asyncio.create_task(
            self._probe_loop(), name="ph:prober"
        )
        if self._debug:
            logger.debug("IPProber: background task started")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        if self._session:
            await self._session.close()
            self._session = None
        if self._debug:
            logger.debug("IPProber: background task stopped")

    # ------------------------------------------------------------------
    # Probe loop
    # ------------------------------------------------------------------

    async def _probe_loop(self) -> None:
        """Probe all addresses concurrently once per interval."""
        while self._running:
            if self._debug:
                logger.debug("IPProber: starting probe round for %d address(es)", len(self._addresses))
            await asyncio.gather(
                *[self._probe_address(addr) for addr in self._addresses],
                return_exceptions=True,
            )
            if self._debug:
                logger.trace(  # type: ignore[attr-defined]
                    "IPProber: probe round complete, sleeping %.0fs", self._interval
                )
            await asyncio.sleep(self._interval)

    async def _probe_address(self, address: str) -> None:
        """Run a single probe through one proxy IP and record the result."""
        url = next(self._url_cycles[address])
        proxy_url = f"http://{address}"
        start = time.monotonic()
        reason: str | None = None

        if self._debug:
            logger.trace(  # type: ignore[attr-defined]
                "IPProber: probing %s via %s", url, address
            )

        try:
            async with self._session.get(  # type: ignore[union-attr]
                url,
                proxy=proxy_url,
                proxy_auth=self._auth_map.get(address),
                timeout=aiohttp.ClientTimeout(total=self._timeout),
                allow_redirects=True,
                ssl=False,  # we're testing connectivity, not certificate validity
            ) as resp:
                    duration = time.monotonic() - start
                    if resp.status < 500:
                        # Any non-5xx response means the proxy IP is reachable
                        # and forwarding traffic — treat as success.
                        _record_probe_success(address, duration)
                        if self._debug:
                            logger.debug(
                                "IPProber: %s via %s → %d (%.3fs)",
                                url, address, resp.status, duration,
                            )
                    else:
                        reason = "http_error"
                        duration = time.monotonic() - start
                        _record_probe_failure(address, reason, duration)
                        logger.warning(
                            "IPProber: %s via %s → %d (%.3fs)",
                            url, address, resp.status, duration,
                        )

        except asyncio.TimeoutError:
            reason = "timeout"
            duration = time.monotonic() - start
            _record_probe_failure(address, reason, duration)
            logger.warning("IPProber: %s via %s timed out after %.1fs", url, address, duration)

        except aiohttp.ClientProxyConnectionError:
            reason = "proxy_unreachable"
            duration = time.monotonic() - start
            _record_probe_failure(address, reason, duration)
            logger.warning("IPProber: could not connect to proxy %s", address)

        except aiohttp.ClientError as exc:
            reason = "connection_error"
            duration = time.monotonic() - start
            _record_probe_failure(address, reason, duration)
            logger.warning("IPProber: %s via %s — %s: %s", url, address, type(exc).__name__, exc)


# ---------------------------------------------------------------------------
# Metric helpers — delegate to the singleton collector
# ---------------------------------------------------------------------------

def _record_probe_success(address: str, duration: float) -> None:
    m = get_metrics()
    m.record_probe_success(address, duration)


def _record_probe_failure(address: str, reason: str, duration: float) -> None:
    m = get_metrics()
    m.record_probe_failure(address, reason, duration)
