"""Shared permission-check helper for GraphQL resolvers."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from strawberry.types import Info
    from ..auth import Permission


def require_permission(info: "Info", permission: "Permission") -> None:
    """Raise PermissionError if the context user lacks *permission*.

    No-op when auth is disabled (``auth_config`` is None or not enabled).
    """
    from ..auth import get_permissions

    ctx = info.context
    if ctx.auth_config is None or not ctx.auth_config.enabled:
        return
    perms = get_permissions(ctx.user.role, ctx.auth_config)
    if permission not in perms:
        raise PermissionError(
            f"Permission denied: '{permission.value}' required, "
            f"role '{ctx.user.role}' has {[p.value for p in perms]}"
        )
