"""Shared permission-check helper for GraphQL resolvers."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from strawberry.types import Info
    from proxy_hopper.auth import Permission


def require_permission(info: "Info", permission: "Permission") -> None:
    """Raise PermissionError if the context user lacks *permission*, or if
    this is a write and the deployment is running in read-only mode
    (server.adminReadOnly) — a deployment-level lockout independent of and
    checked before any role-based auth, so it applies even with auth
    disabled entirely.
    """
    from proxy_hopper.auth import Permission, get_permissions

    ctx = info.context
    if permission == Permission.write and getattr(ctx, "read_only", False):
        raise PermissionError(
            "This deployment is running in read-only mode (server.adminReadOnly) — "
            "config mutations are disabled; all config comes from the YAML file."
        )
    if ctx.auth_config is None or not ctx.auth_config.enabled:
        return
    perms = get_permissions(ctx.user.role, ctx.auth_config)
    if permission not in perms:
        raise PermissionError(
            f"Permission denied: '{permission.value}' required, "
            f"role '{ctx.user.role}' has {[p.value for p in perms]}"
        )
