"""ProxyHopperClient — IP-pinned HTTP helper for token server implementations.

Routes requests through Proxy Hopper using X-ProxyHopper-Force-IP so that
token acquisition originates from the same upstream proxy IP that will use
the resulting token. This is required when the auth endpoint checks or ties
the session to the requesting IP.
"""

from __future__ import annotations

import aiohttp


_FORCE_IP_HEADER = "X-ProxyHopper-Force-IP"


class ProxyHopperClient:
    """Thin aiohttp wrapper that routes requests through Proxy Hopper,
    pinning to a specific upstream proxy IP via X-ProxyHopper-Force-IP.

    Usage::

        client = ProxyHopperClient(proxy_url=req.proxy_url)
        resp = await client.post(
            "https://example.com/auth/token",
            via_ip=f"{req.ip}:{req.port}",
            headers={"User-Agent": req.profile.user_agent},
            data=b'{"grant_type": "client_credentials"}',
        )
    """

    def __init__(self, proxy_url: str) -> None:
        """
        Args:
            proxy_url: Proxy Hopper's proxy listener, e.g. ``"http://proxy-hopper:8085"``.
        """
        self._proxy_url = proxy_url

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
    ) -> aiohttp.ClientResponse:
        """Send *method* *url* through Proxy Hopper pinned to *via_ip*.

        Args:
            method:  HTTP method (``"GET"``, ``"POST"``, …).
            url:     Target URL.
            via_ip:  ``"host:port"`` of the upstream proxy IP to pin to.
                     Must match an IP registered in the target's pool.
            headers: Additional request headers.
            data:    Raw request body bytes.
            timeout: Per-request timeout in seconds.
        """
        merged_headers = dict(headers or {})
        merged_headers[_FORCE_IP_HEADER] = via_ip

        timeout_obj = aiohttp.ClientTimeout(total=timeout)
        async with aiohttp.ClientSession(timeout=timeout_obj) as session:
            return await session.request(
                method,
                url,
                headers=merged_headers,
                data=data,
                proxy=self._proxy_url,
                **kwargs,
            )

    async def get(
        self, url: str, *, via_ip: str, **kwargs
    ) -> aiohttp.ClientResponse:
        return await self.request("GET", url, via_ip=via_ip, **kwargs)

    async def post(
        self, url: str, *, via_ip: str, **kwargs
    ) -> aiohttp.ClientResponse:
        return await self.request("POST", url, via_ip=via_ip, **kwargs)
