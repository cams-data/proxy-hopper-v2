"""Programmatic Alembic entry points — wraps alembic.command, not the CLI binary.

Used by both the `migrate` click command (for operators / the Docker image)
and the Helm migration Job/initContainer (which calls the same `migrate`
command inside the container).
"""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

_SCRIPT_LOCATION = Path(__file__).parent / "alembic"


def _config(url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(_SCRIPT_LOCATION))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def upgrade(url: str, revision: str = "head") -> None:
    from alembic import command

    command.upgrade(_config(url), revision)


def downgrade(url: str, revision: str = "base") -> None:
    from alembic import command

    command.downgrade(_config(url), revision)


def heads(url: str) -> list[str]:
    """Return the current head revision(s) — more than one means a branch
    point that needs merging (the CI multi-head guard checks this)."""
    script = ScriptDirectory.from_config(_config(url))
    return list(script.get_heads())
