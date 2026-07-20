from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RuntimeConfig:
    headless: bool = True
    accept_downloads: bool = True


@dataclass(frozen=True)
class SharedAuthRuntime:
    """Shared browser/session auth runtime config for system adapters."""

    config: RuntimeConfig = RuntimeConfig()
