"""Runtime data models shared between the core and backend packages."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum, auto


# ---------------------------------------------------------------------------
# IP state — mutated only by the backend to avoid races
# ---------------------------------------------------------------------------

@dataclass
class IPState:
    """State for a single external proxy IP address."""
    host: str
    port: int
    consecutive_failures: int = 0
    last_used_at: float = field(default_factory=lambda: 0.0)
    quarantined_until: float = field(default_factory=lambda: 0.0)

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"

    def reset_failures(self) -> None:
        self.consecutive_failures = 0

    def record_failure(self) -> None:
        self.consecutive_failures += 1

    def record_success(self) -> None:
        self.consecutive_failures = 0
        self.last_used_at = time.monotonic()


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
    failure_count: int = 0

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
            failure_count=self.failure_count + 1,
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
# Return reasons — communicated from TargetManager to the backend
# ---------------------------------------------------------------------------

class ReturnReason(Enum):
    SUCCESS = auto()
    RATE_LIMITED = auto()        # 429 — request should be retried
    SERVER_ERROR = auto()        # 5xx
    CONNECTION_ERROR = auto()    # network failure
    FROM_QUARANTINE = auto()     # internal use by backends
