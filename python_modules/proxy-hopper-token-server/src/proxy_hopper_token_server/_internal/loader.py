"""Import-path resolution for the ph-token-server CLI.

Resolves a ``module:attribute`` string to a ``TokenServer`` instance,
following the rules in the spec (§8):

1. Import the module.
2. Resolve the attribute.
3. ``TokenServer`` instance  → use directly.
4. ``TokenProvider`` instance → wrap in ``TokenServer(provider=...)``.
5. ``TokenProvider`` subclass → instantiate with no args, then wrap.
6. Anything else → raise ``ImportError`` with a descriptive message.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path


def resolve(import_path: str) -> "TokenServer":  # noqa: F821
    """Resolve *import_path* to a ``TokenServer`` instance.

    Args:
        import_path: ``"module.path:AttributeName"`` string.

    Returns:
        A ready-to-use ``TokenServer`` instance.

    Raises:
        SystemExit: On any resolution error (prints a user-friendly message).
    """
    from proxy_hopper_token_server.provider import TokenProvider
    from proxy_hopper_token_server.server import TokenServer

    if ":" not in import_path:
        _die(
            f"Invalid import path {import_path!r}. "
            "Expected format: 'module.path:AttributeName'"
        )

    module_path, attr_name = import_path.rsplit(":", 1)

    # Ensure the current working directory is on sys.path so local packages
    # can be imported without installation.
    cwd = str(Path.cwd())
    if cwd not in sys.path:
        sys.path.insert(0, cwd)

    try:
        module = importlib.import_module(module_path)
    except ModuleNotFoundError as exc:
        _die(f"Cannot import module {module_path!r}: {exc}")

    try:
        obj = getattr(module, attr_name)
    except AttributeError:
        _die(
            f"Module {module_path!r} has no attribute {attr_name!r}. "
            f"Available: {[n for n in dir(module) if not n.startswith('_')]}"
        )

    if isinstance(obj, TokenServer):
        return obj

    if isinstance(obj, TokenProvider):
        return TokenServer(provider=obj)

    if isinstance(obj, type) and issubclass(obj, TokenProvider) and obj is not TokenProvider:
        try:
            instance = obj()
        except Exception as exc:
            _die(
                f"Could not instantiate {attr_name} with no arguments: {exc}. "
                "Pass a pre-instantiated provider or server instead."
            )
        return TokenServer(provider=instance)

    _die(
        f"{import_path!r} resolved to {type(obj)!r}, which is not a "
        "TokenServer instance, TokenProvider instance, or TokenProvider subclass."
    )


def _die(msg: str) -> None:
    import click
    raise click.ClickException(msg)
