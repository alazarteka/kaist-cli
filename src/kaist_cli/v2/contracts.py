from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


Capability = Literal["planned", "partial", "full", "degraded"]
Source = Literal["bootstrap", "probe", "moodle_mobile", "moodle_ajax", "html", "browser", "cache", "mixed"]


@dataclass(frozen=True)
class CommandResult:
    data: Any
    source: Source
    capability: Capability


@dataclass
class CommandError(Exception):
    code: str
    message: str
    hint: str | None = None
    exit_code: int = 50
    retryable: bool = False

    def __str__(self) -> str:
        return self.message
