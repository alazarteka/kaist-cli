from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class RewriteStatus:
    phase: str
    branch: str
    interface_style: str
    auth_strategy: tuple[str, ...]
    provider_order: tuple[str, ...]
    next_moves: tuple[str, ...]


@dataclass(frozen=True)
class DoctorReport:
    status: str
    focus: tuple[str, ...]
    blockers: tuple[str, ...]
    rules: tuple[str, ...]


class CapabilityProbe(Protocol):
    def status(self) -> RewriteStatus: ...

    def doctor(self) -> DoctorReport: ...

    def probe_plan(self) -> dict[str, object]: ...

