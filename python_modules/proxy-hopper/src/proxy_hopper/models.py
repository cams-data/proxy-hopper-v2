"""Runtime data models shared between the core and backend packages."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Pending request — sits in each TargetManager's local request queue
# ---------------------------------------------------------------------------

@dataclass
class PendingRequest:
    """A proxied request waiting for an available IP."""
    method: str
    url: str
    headers: dict[str, str]
    body: bytes | None
    future: asyncio.Future         # resolved with ProxyResponse or an exception
    arrival_time: float            # monotonic seconds
    max_queue_wait: float
    num_retries: int
    tag: str = ""                  # X-Proxy-Hopper-Tag value (propagated to metrics)
    failure_count: int = 0
    header_overrides: dict[str, str] = field(default_factory=dict)  # X-Proxy-Hopper-{Header} values
    force_ip: str = ""             # X-Proxy-Hopper-Force-IP: host:port — bypass pool selection

    @property
    def deadline(self) -> float:
        return self.arrival_time + self.max_queue_wait

    def is_expired(self) -> bool:
        return time.monotonic() >= self.deadline

    def time_remaining(self) -> float:
        return max(0.0, self.deadline - time.monotonic())

    def can_retry(self) -> bool:
        return self.failure_count < self.num_retries

    def clone_for_retry(self) -> "PendingRequest":
        return PendingRequest(
            method=self.method,
            url=self.url,
            headers=self.headers,
            body=self.body,
            future=self.future,
            arrival_time=self.arrival_time,
            max_queue_wait=self.max_queue_wait,
            num_retries=self.num_retries,
            tag=self.tag,
            failure_count=self.failure_count + 1,
            header_overrides=self.header_overrides,
            force_ip=self.force_ip,
        )


# ---------------------------------------------------------------------------
# Response returned through the future
# ---------------------------------------------------------------------------

@dataclass
class ProxyResponse:
    status: int
    headers: dict[str, str]
    body: bytes


# ---------------------------------------------------------------------------
# Shared HTTP constants
# ---------------------------------------------------------------------------

#: Headers that must not be forwarded between client↔proxy or proxy↔upstream.
#: Defined once here; imported by handlers.py and target_manager.py.
HOP_BY_HOP_HEADERS = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "proxy-connection", "te", "trailers", "transfer-encoding", "upgrade",
})
