from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RuntimeConfig:
    headless: bool = True
    accept_downloads: bool = True


@dataclass(frozen=True)
class SharedAuthRuntime:
    """
    Shared runtime marker for systems that use browser/session auth.

    System adapters can compose their own runtime implementations while sharing
    this common config object.
    """

    config: RuntimeConfig = RuntimeConfig()
