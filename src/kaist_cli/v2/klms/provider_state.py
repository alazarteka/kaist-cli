from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..contracts import Capability, CommandResult, Source


@dataclass(frozen=True)
class ProviderLoad:
    items: list[dict[str, Any]]
    source: Source
    capability: Capability
    freshness_mode: str
    cache_hit: bool
    stale: bool
    fetched_at: str | None
    expires_at: str | None
    refresh_attempted: bool
    ok: bool = True
    warnings: tuple[dict[str, Any], ...] = ()

    def to_command_result(self) -> CommandResult:
        return CommandResult(data=self.items, source=self.source, capability=self.capability)

    def provider_status(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": self.ok,
            "source": self.source,
            "capability": self.capability,
            "count": len(self.items),
            "freshness_mode": self.freshness_mode,
            "cache_hit": self.cache_hit,
            "stale": self.stale,
            "fetched_at": self.fetched_at,
            "expires_at": self.expires_at,
            "refresh_attempted": self.refresh_attempted,
        }
        if self.warnings:
            payload["warning_codes"] = [str(warning.get("code") or "") for warning in self.warnings if str(warning.get("code") or "").strip()]
        return payload

    def provider_warnings(self, provider: str) -> list[dict[str, Any]]:
        return [
            {
                "provider": provider,
                **warning,
            }
            for warning in self.warnings
        ]
