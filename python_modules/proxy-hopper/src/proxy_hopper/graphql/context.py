"""GraphQL request context — carries the repository and authenticated user."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from ..auth import AuthenticatedUser
    from ..config import AuthConfig
    from ..repository import ProxyRepository


@dataclass
class Context:
    repo: "ProxyRepository"
    user: "AuthenticatedUser"
    auth_config: Optional["AuthConfig"]
