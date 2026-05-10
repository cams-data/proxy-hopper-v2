"""GraphQL request context — carries the repository and authenticated user."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from strawberry.fastapi import BaseContext

if TYPE_CHECKING:
    from proxy_hopper.auth import AuthenticatedUser
    from proxy_hopper.config import AuthConfig
    from proxy_hopper.repository import ProxyRepository


class Context(BaseContext):
    def __init__(
        self,
        repo: "ProxyRepository",
        user: "AuthenticatedUser",
        auth_config: Optional["AuthConfig"],
    ) -> None:
        super().__init__()
        self.repo = repo
        self.user = user
        self.auth_config = auth_config
