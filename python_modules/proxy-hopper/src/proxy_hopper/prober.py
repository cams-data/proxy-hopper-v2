"""Background IP health prober.

Periodically tests every known proxy IP address against a set of well-known
endpoints and records the results as Prometheus metrics.  Completely
independent of the pool, backend, and target machinery — it has no side
effects on IP state and no knowledge of targets.

The prober works from the full set of ProxyProvider definitions.  Each
provider's IPs are probed using that provider's credentials, and results are
tagged with the provider name and region for metric filtering.  If no
providers are configured (inline ipList only), the prober falls back to
iterating target IPs with no provider metadata.

Metrics written
---------------
  proxy_hopper_probe_success_total{address, provider, region}            Counter
  proxy_hopper_probe_failure_total{address, provider, region, reason}    Counter
  proxy_hopper_probe_duration_seconds{address, provider, region}         Histogram
  proxy_hopper_ip_reachable{address, provider, region}                   Gauge  (1=up, 0=down)
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import time
from typing import Sequence

import aiohttp

from .config import ProxyProvider, TargetConfig
from .metrics import get_metrics

logger = logging.getLogger(__name__)

# Default probe endpoints — plain HTTP so they work through HTTP-only proxies
# that do not support CONNECT tunnelling.  HTTPS probe URLs would require the
# proxy to establish a TLS tunnel, which plain HTTP proxies reject.
DEFAULT_PROBE_URLS: tuple[str, ...] = (
    "http://1.1.1.1",
    "http://www.google.com",
)

_DEFAULT_PORT = 8080


class _ProbeEntry:
    """Internal: everything needed to probe one IP address."""
    __slots__ = ("address", "provider", "region", "auth")

    def __init__(
        self,
        address: str,
        provider: str = "",
        region: str = "",
        auth: aiohttp.BasicAuth | None = None,
    ) -> None:
        self.address = address
        self.provider = provider
        self.region = region
        self.auth = auth


class IPProber:
    """Background task that health-checks proxy IPs and exports metrics.

    Parameters
    ----------
    providers:
        All configured proxy providers.  IPs are deduplicated; each address
        is probed with the credentials and region tag of its provider.
    targets:
        Fallback — used only when there are no providers (pure inline ipList
        configs).  IPs are deduplicated; first target claiming an address wins.
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
        providers: Sequence[ProxyProvider] = (),
        targets: Sequence[TargetConfig] = (),
        probe_urls: Sequence[str] = DEFAULT_PROBE_URLS,
        interval: float = 60.0,
        timeout: float = 10.0,
        debug: bool = False,
    ) -> None:
        entries: list[_ProbeEntry] = []
        seen: set[str] = set()

        # Primary path — derive probes from providers
        for provider in providers:
            auth = (
                aiohttp.BasicAuth(provider.auth.username, provider.auth.password)
                if provider.auth is not None
                else None
            )
            for host, port in provider.resolved_ip_list(_DEFAULT_PORT):
                address = f"{host}:{port}"
                if address not in seen:
                    seen.add(address)
                    entries.append(_ProbeEntry(
                        address=address,
                        provider=provider.name,
                        region=provider.region_tag or "",
                        auth=auth,
                    ))

        # Fallback path — inline IPs from targets (no provider metadata)
        for target in targets:
            for host, port in target.resolved_ip_list():
                address = f"{host}:{port}"
                if address not in seen:
                    seen.add(address)
                    entries.append(_ProbeEntry(address=address))

        self._entries = entries
        self._probe_urls = list(probe_urls)
        self._interval = interval
        self._timeout = timeout
        self._debug = debug
        self._task: asyncio.Task | None = None
        self._session: aiohttp.ClientSession | None = None
        self._running = False
        # Rotate probe URLs independently per address
        self._url_cycles = {
            e.address: itertools.cycle(self._probe_urls) for e in self._entries
        }

        logger.info(
            "IPProber: initialised with %d unique address(es) across %d provider(s), interval=%.0fs, urls=%s",
            len(self._entries), len(providers), self._interval, self._probe_urls,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if not self._entries:
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
                logger.debug("IPProber: starting probe round for %d address(es)", len(self._entries))
            await asyncio.gather(
                *[self._probe_address(entry) for entry in self._entries],
                return_exceptions=True,
            )
            if self._debug:
                logger.trace(  # type: ignore[attr-defined]
                    "IPProber: probe round complete, sleeping %.0fs", self._interval
                )
            await asyncio.sleep(self._interval)

    async def _probe_address(self, entry: _ProbeEntry) -> None:
        """Run a single probe through one proxy IP and record the result."""
        url = next(self._url_cycles[entry.address])
        proxy_url = f"http://{entry.address}"
        start = time.monotonic()
        reason: str | None = None

        if self._debug:
            logger.trace(  # type: ignore[attr-defined]
                "IPProber: probing %s via %s (provider=%s region=%s)",
                url, entry.address, entry.provider or "-", entry.region or "-",
            )

        try:
            async with self._session.get(  # type: ignore[union-attr]
                url,
                proxy=proxy_url,
                proxy_auth=entry.auth,
                timeout=aiohttp.ClientTimeout(total=self._timeout),
                allow_redirects=True,
                ssl=False,  # we're testing connectivity, not certificate validity
            ) as resp:
                    duration = time.monotonic() - start
                    if resp.status < 500:
                        # Any non-5xx response means the proxy IP is reachable
                        # and forwarding traffic — treat as success.
                        get_metrics().record_probe_success(
                            entry.address, duration,
                            provider=entry.provider, region=entry.region,
                        )
                        if self._debug:
                            logger.debug(
                                "IPProber: %s via %s → %d (%.3fs)",
                                url, entry.address, resp.status, duration,
                            )
                    else:
                        reason = "http_error"
                        duration = time.monotonic() - start
                        get_metrics().record_probe_failure(
                            entry.address, reason, duration,
                            provider=entry.provider, region=entry.region,
                        )
                        logger.warning(
                            "IPProber: %s via %s → %d (%.3fs)",
                            url, entry.address, resp.status, duration,
                        )

        except asyncio.TimeoutError:
            reason = "timeout"
            duration = time.monotonic() - start
            get_metrics().record_probe_failure(
                entry.address, reason, duration,
                provider=entry.provider, region=entry.region,
            )
            logger.warning("IPProber: %s via %s timed out after %.1fs", url, entry.address, duration)

        except aiohttp.ClientProxyConnectionError:
            reason = "proxy_unreachable"
            duration = time.monotonic() - start
            get_metrics().record_probe_failure(
                entry.address, reason, duration,
                provider=entry.provider, region=entry.region,
            )
            logger.warning("IPProber: could not connect to proxy %s", entry.address)

        except aiohttp.ClientError as exc:
            reason = "connection_error"
            duration = time.monotonic() - start
            get_metrics().record_probe_failure(
                entry.address, reason, duration,
                provider=entry.provider, region=entry.region,
            )
            logger.warning("IPProber: %s via %s — %s: %s", url, entry.address, type(exc).__name__, exc)
