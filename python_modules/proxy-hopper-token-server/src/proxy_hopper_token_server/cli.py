"""ph-token-server CLI."""

from __future__ import annotations

import click


@click.group()
def main() -> None:
    """Proxy Hopper Token Server — serve a TokenProvider over HTTP."""


@main.command()
@click.argument("import_path")
@click.option("--host", default="0.0.0.0", show_default=True, help="Interface to bind.")
@click.option("--port", default=9000, show_default=True, type=int, help="Port to listen on.")
@click.option("--workers", default=1, show_default=True, type=int, help="Number of uvicorn worker processes.")
@click.option(
    "--log-level",
    default="info",
    show_default=True,
    type=click.Choice(["trace", "debug", "info", "warning", "error"], case_sensitive=False),
)
@click.option("--timeout", default=30.0, show_default=True, type=float,
              help="Hard timeout (seconds) for each get_token call.")
def start(
    import_path: str,
    host: str,
    port: int,
    workers: int,
    log_level: str,
    timeout: float,
) -> None:
    """Start the token server from IMPORT_PATH.

    IMPORT_PATH is a 'module.path:AttributeName' string resolving to a
    TokenServer instance, TokenProvider instance, or TokenProvider subclass.

    \b
    Examples:
      ph-token-server start myapp.tokens:MyProvider
      ph-token-server start myapp.tokens:provider --port 9001
      ph-token-server start myapp.tokens:server --workers 4
    """
    from ._internal.loader import resolve

    server = resolve(import_path)

    # CLI flags override instance defaults.
    server.host = host
    server.port = port
    server.timeout = timeout

    click.echo(f"Serving on http://{host}:{port} (workers={workers})")
    server.run(workers=workers, log_level=log_level)
