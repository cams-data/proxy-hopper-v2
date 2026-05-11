"""TokenProvider abstract base class."""

from __future__ import annotations

from abc import ABC, abstractmethod

from .models import TokenRequest, TokenResponse


class TokenProvider(ABC):
    """Abstract base class for user-provided token acquisition logic.

    Implement ``get_token`` to fetch or refresh an auth token for a given
    (target, ip) pair. The method is called by Proxy Hopper:

    - On startup, to pre-warm tokens for all auth-managed (target, ip) pairs.
    - Whenever ``expires_at - refresh_threshold`` is reached for a token.

    Raising any exception marks the IP as ``AUTH_BROKEN`` for that target and
    triggers the retry / quarantine logic defined in the Proxy Hopper config.
    """

    @abstractmethod
    async def get_token(self, request: TokenRequest) -> TokenResponse:
        """Fetch or refresh a token for the given (target, ip) pair.

        Use ``request.profile`` to build realistic browser-like requests.
        Use ``request.proxy_url`` with ``ProxyHopperClient`` to route token
        acquisition requests through the same upstream proxy IP (see client.py).
        Use ``request.cursor`` to carry opaque state across calls.
        """
        ...
