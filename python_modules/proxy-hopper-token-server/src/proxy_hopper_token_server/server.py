"""TokenServer — wraps a TokenProvider in a FastAPI + uvicorn HTTP server."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .provider import TokenProvider


class TokenServer:
    """Run a ``TokenProvider`` as an HTTP server compatible with Proxy Hopper.

    Args:
        provider: A single ``TokenProvider`` for all targets, or a dict
                  mapping target names to specific providers.
        host:     Interface to bind (default ``"0.0.0.0"``).
        port:     Port to listen on (default ``9000``).
        timeout:  Hard timeout in seconds for each ``get_token`` call.
                  Proxy Hopper's ``authServer.timeoutSeconds`` must be ≤ this.
    """

    def __init__(
        self,
        provider: "TokenProvider | dict[str, TokenProvider]",
        host: str = "0.0.0.0",
        port: int = 9000,
        timeout: float = 30.0,
    ) -> None:
        self._provider = provider
        self.host = host
        self.port = port
        self.timeout = timeout

    def _build_app(self):
        from ._internal.app import create_app
        return create_app(self._provider, timeout=self.timeout)

    def run(self, workers: int = 1, log_level: str = "info") -> None:
        """Start the server synchronously (blocks). Suitable for ``__main__`` scripts."""
        import uvicorn
        uvicorn.run(
            self._build_app(),
            host=self.host,
            port=self.port,
            workers=workers,
            log_level=log_level,
        )

    async def start(self, log_level: str = "info") -> None:
        """Start the server as a coroutine (single worker).

        For embedding in an existing asyncio event loop.
        """
        import uvicorn
        config = uvicorn.Config(
            self._build_app(),
            host=self.host,
            port=self.port,
            log_level=log_level,
        )
        server = uvicorn.Server(config)
        await server.serve()
