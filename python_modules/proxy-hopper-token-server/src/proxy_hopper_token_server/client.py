"""ProxyHopperClient — IP-pinned HTTP helper for token server implementations.

Routes requests *through* a running Proxy Hopper instance, in the same
header-based forwarding mode Proxy Hopper's core uses for all traffic (see
``proxy_hopper.handlers.ForwardingHandler``) — there is no other mode to use.
The request is sent directly to Proxy Hopper's own address with
``X-Proxy-Hopper-Target`` naming the real destination, and
``X-ProxyHopper-Force-IP`` pinning it to one specific upstream proxy IP, so
that token acquisition originates from the same IP that will later use the
resulting token. This matters when the auth endpoint checks or ties the
session to the requesting IP.

Earlier versions of this client used aiohttp's ``proxy=`` keyword argument,
which speaks classic HTTP-proxy / CONNECT-tunnel semantics. Proxy Hopper
dropped both of those modes early in the project — ``proxy=`` requests never
reached ``ForwardingHandler`` and always failed. This client now builds a
forwarding-mode request by hand instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping
from urllib.parse import urlparse

import aiohttp

_TARGET_HEADER = "X-Proxy-Hopper-Target"
_FORCE_IP_HEADER = "X-ProxyHopper-Force-IP"


@dataclass(frozen=True)
class ProxyHopperResponse:
    """A fully-buffered response from a request routed through Proxy Hopper.

    The underlying aiohttp session is closed before this is returned, so the
    body is read eagerly and cached here — unlike ``aiohttp.ClientResponse``,
    ``read()`` never touches the network and is safe to call any number of
    times, including after the client call has returned.
    """

    status: int
    headers: Mapping[str, str]
    body: bytes

    async def read(self) -> bytes:
        return self.body

    def text(self, encoding: str = "utf-8", errors: str = "replace") -> str:
        return self.body.decode(encoding, errors=errors)


class ProxyHopperClient:
    """Thin HTTP helper that routes requests through a Proxy Hopper instance,
    pinning each one to a specific upstream proxy IP via
    ``X-ProxyHopper-Force-IP``.

    Usage::

        client = ProxyHopperClient(proxy_url=req.proxy_url)
        resp = await client.post(
            "https://example.com/auth/token",
            via_ip=f"{req.ip}:{req.port}",
            headers={"User-Agent": req.profile.user_agent},
            data=b'{"grant_type": "client_credentials"}',
        )
        body = await resp.read()
    """

    def __init__(self, proxy_url: str) -> None:
        """
        Args:
            proxy_url: Proxy Hopper's proxy listener, e.g. ``"http://proxy-hopper:8080"``.
                       This is ``request.proxy_url`` from the ``/token`` request body,
                       only present when ``server.authServer.exposeProxyUrl: true``.
        """
        self._proxy_url = proxy_url.rstrip("/")

    async def request(
        self,
        method: str,
        url: str,
        *,
        via_ip: str,
        headers: dict[str, str] | None = None,
        data: bytes | None = None,
        timeout: float = 10.0,
        **kwargs,
    ) -> ProxyHopperResponse:
        """Send *method* *url* through Proxy Hopper pinned to *via_ip*.

        Args:
            method:  HTTP method (``"GET"``, ``"POST"``, …).
            url:     Real destination URL (scheme + host + path + query).
            via_ip:  ``"host:port"`` of the upstream proxy IP to pin to.
                     Must match an IP already registered in the target's pool.
            headers: Additional request headers.
            data:    Raw request body bytes.
            timeout: Per-request timeout in seconds.
        """
        parsed = urlparse(url)
        target = f"{parsed.scheme}://{parsed.netloc}"
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        request_url = f"{self._proxy_url}{path}"

        merged_headers = dict(headers or {})
        merged_headers[_TARGET_HEADER] = target
        merged_headers[_FORCE_IP_HEADER] = via_ip

        timeout_obj = aiohttp.ClientTimeout(total=timeout)
        async with aiohttp.ClientSession(timeout=timeout_obj) as session:
            async with session.request(
                method,
                request_url,
                headers=merged_headers,
                data=data,
                **kwargs,
            ) as resp:
                body = await resp.read()
                return ProxyHopperResponse(status=resp.status, headers=resp.headers, body=body)

    async def get(
        self, url: str, *, via_ip: str, **kwargs
    ) -> ProxyHopperResponse:
        return await self.request("GET", url, via_ip=via_ip, **kwargs)

    async def post(
        self, url: str, *, via_ip: str, **kwargs
    ) -> ProxyHopperResponse:
        return await self.request("POST", url, via_ip=via_ip, **kwargs)
