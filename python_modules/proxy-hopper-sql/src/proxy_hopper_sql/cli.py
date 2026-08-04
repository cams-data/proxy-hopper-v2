"""`migrate` command — grafted onto proxy-hopper's core CLI group the same
way proxy-hopper-webserver's `admin` command is (see proxy_hopper.cli).
"""

from __future__ import annotations

import sys

import click

from .migrations import upgrade as _upgrade


@click.command("migrate")
@click.option("--database-url", required=False, default=None,
              envvar="PROXY_HOPPER_CONFIG_STORE_URL",
              help="SQLAlchemy URL, e.g. sqlite+aiosqlite:///./data/config.db "
                   "or postgresql+asyncpg://user:pass@host/db.")
def migrate(database_url: str | None) -> None:
    """Apply ConfigStore database migrations up to head."""
    if database_url is None:
        click.echo(
            "Error: --database-url / PROXY_HOPPER_CONFIG_STORE_URL is required.",
            err=True,
        )
        sys.exit(1)

    _upgrade(database_url)
    click.echo(f"Migrated {database_url!r} to head.")
