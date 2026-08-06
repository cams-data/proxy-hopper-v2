"""GraphQL request context — carries the repository and authenticated user."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from strawberry.fastapi import BaseContext

if TYPE_CHECKING:
    from proxy_hopper.app_metrics import AppMetricsStore
    from proxy_hopper.auth import AuthenticatedUser
    from proxy_hopper.config import AuthConfig
    from proxy_hopper.repository import ProxyRepository


class Context(BaseContext):
    def __init__(
        self,
        repo: "ProxyRepository",
        user: "AuthenticatedUser",
        auth_config: Optional["AuthConfig"],
        app_metrics: Optional["AppMetricsStore"] = None,
        prometheus_url: Optional[str] = None,
        read_only: bool = False,
    ) -> None:
        super().__init__()
        self.repo = repo
        self.user = user
        self.auth_config = auth_config
        # At most one of these is set — see queries.py's `target_metrics`.
        self.app_metrics = app_metrics
        self.prometheus_url = prometheus_url
        # Deployment-level lockout on mutations — see server.adminReadOnly
        # and _auth.py's require_permission. Independent of auth_config.
        self.read_only = read_only
