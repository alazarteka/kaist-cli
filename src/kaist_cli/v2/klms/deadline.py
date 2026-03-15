from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass(frozen=True)
class RefreshDeadline:
    started_at_monotonic: float
    soft_deadline_monotonic: float
    hard_deadline_monotonic: float

    @classmethod
    def start(cls, *, soft_seconds: float = 12.0, hard_seconds: float = 15.0) -> "RefreshDeadline":
        now = time.monotonic()
        return cls(
            started_at_monotonic=now,
            soft_deadline_monotonic=now + max(0.1, soft_seconds),
            hard_deadline_monotonic=now + max(0.1, max(hard_seconds, soft_seconds)),
        )

    def soft_expired(self) -> bool:
        return time.monotonic() >= self.soft_deadline_monotonic

    def hard_expired(self) -> bool:
        return time.monotonic() >= self.hard_deadline_monotonic

    def remaining_soft(self) -> float:
        return max(0.0, self.soft_deadline_monotonic - time.monotonic())

    def remaining_hard(self) -> float:
        return max(0.0, self.hard_deadline_monotonic - time.monotonic())

    def request_timeout(self, default_seconds: float, *, use_soft: bool = True) -> float:
        remaining = self.remaining_soft() if use_soft else self.remaining_hard()
        if remaining <= 0:
            raise TimeoutError("Interactive refresh budget expired.")
        return max(1.0, min(float(default_seconds), remaining))
