from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ...core.timeutil import cache_is_fresh_enough, iso_from_epoch_seconds, utc_now_iso
from ..contracts import Capability, CommandError, CommandResult, Source
from .deadline import RefreshDeadline
from .browser_types import BrowserContextLike


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
        if not self.ok:
            status = "failed"
        elif self.refresh_attempted:
            status = "refreshed"
        elif self.cache_hit:
            status = "cache_hit"
        else:
            status = "skipped"
        payload: dict[str, Any] = {
            "ok": self.ok,
            "status": status,
            "source": self.source,
            "capability": self.capability,
            "count": len(self.items),
            "item_count": len(self.items),
            "freshness_mode": self.freshness_mode,
            "cache_hit": self.cache_hit,
            "stale": self.stale,
            "fetched_at": self.fetched_at,
            "expires_at": self.expires_at,
            "refresh_attempted": self.refresh_attempted,
        }
        if self.warnings:
            payload["warning_codes"] = [str(warning.get("code") or "") for warning in self.warnings if str(warning.get("code") or "").strip()]
            payload["warnings"] = [dict(warning) for warning in self.warnings]
        return payload

    def provider_warnings(self, provider: str) -> list[dict[str, Any]]:
        return [
            {
                "provider": provider,
                **warning,
            }
            for warning in self.warnings
        ]


def provider_warning(code: str, message: str, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"code": code, "message": message}
    payload.update(extra)
    return payload


@dataclass(frozen=True)
class CachedProviderSnapshot:
    items: list[dict[str, Any]]  # already to_dict'd and limited/filtered
    cache_entry: dict[str, Any] | None
    source: Source
    capability: Capability = "partial"
    empty_fail_source: Source = "html"


def _resource_title(resource_label: str) -> str:
    return str(resource_label).strip().capitalize()


def load_cached_or_refresh(
    *,
    prefer_cache: bool,
    deadline: RefreshDeadline | None,
    snapshot: CachedProviderSnapshot,
    refresh: Callable[[], tuple[list[dict[str, Any]], Source, Capability]],
    resource_label: str,  # "file" or "notice"
    fresh_timestamps: Callable[[], tuple[str | None, str | None]] | None = None,
) -> ProviderLoad:
    cache_entry = snapshot.cache_entry
    cached_items = snapshot.items
    cache_fresh_enough = cache_is_fresh_enough(cache_entry)
    resource = str(resource_label).strip() or "item"
    resource_title = _resource_title(resource)

    def _cached_load(*, refresh_attempted: bool, warnings: list[dict[str, Any]]) -> ProviderLoad:
        assert cache_entry is not None
        return ProviderLoad(
            items=list(cached_items),
            source=snapshot.source,
            capability=snapshot.capability,
            freshness_mode="cache",
            cache_hit=True,
            stale=bool(cache_entry.get("stale")),
            fetched_at=iso_from_epoch_seconds(cache_entry.get("stored_at")),
            expires_at=iso_from_epoch_seconds(cache_entry.get("expires_at")),
            refresh_attempted=refresh_attempted,
            ok=True,
            warnings=tuple(warnings),
        )

    def _empty_fail(*, refresh_attempted: bool, warnings: tuple[dict[str, Any], ...]) -> ProviderLoad:
        return ProviderLoad(
            items=[],
            source=snapshot.empty_fail_source,
            capability="degraded",
            freshness_mode="live",
            cache_hit=False,
            stale=False,
            fetched_at=None,
            expires_at=None,
            refresh_attempted=refresh_attempted,
            ok=False,
            warnings=warnings,
        )

    if prefer_cache and cache_entry is not None and (not bool(cache_entry.get("stale")) or cache_fresh_enough):
        return _cached_load(refresh_attempted=False, warnings=[])

    if deadline is not None and deadline.hard_expired():
        if cache_entry is not None and cached_items:
            warnings: list[dict[str, Any]] = []
            if not cache_is_fresh_enough(cache_entry):
                warnings.append(
                    provider_warning(
                        "LIVE_REFRESH_TIMEOUT",
                        f"Interactive refresh budget expired before {resource} refresh completed.",
                    )
                )
            if bool(cache_entry.get("stale")):
                warnings.insert(
                    0,
                    provider_warning(
                        "STALE_CACHE",
                        f"Returning stale {resource} cache because live refresh could not finish in time.",
                    ),
                )
            return _cached_load(refresh_attempted=True, warnings=warnings)
        return _empty_fail(
            refresh_attempted=False,
            warnings=(
                provider_warning(
                    "LIVE_REFRESH_TIMEOUT",
                    f"Interactive refresh budget expired before {resource} refresh started.",
                ),
            ),
        )

    try:
        live_items, live_source, live_capability = refresh()
    except TimeoutError:
        if cache_entry is not None and cached_items:
            warnings = []
            if not cache_is_fresh_enough(cache_entry):
                warnings.append(
                    provider_warning(
                        "LIVE_REFRESH_TIMEOUT",
                        f"{resource_title} refresh exceeded the interactive deadline.",
                    )
                )
            if bool(cache_entry.get("stale")):
                warnings.insert(
                    0,
                    provider_warning(
                        "STALE_CACHE",
                        f"Returning stale {resource} cache because live refresh timed out.",
                    ),
                )
            return _cached_load(refresh_attempted=True, warnings=warnings)
        return _empty_fail(
            refresh_attempted=True,
            warnings=(
                provider_warning(
                    "LIVE_REFRESH_TIMEOUT",
                    f"{resource_title} refresh exceeded the interactive deadline.",
                ),
            ),
        )
    except CommandError:
        raise
    except Exception as exc:
        if cache_entry is not None and cached_items:
            warnings = []
            if not cache_is_fresh_enough(cache_entry):
                warnings.append(
                    provider_warning(
                        "LIVE_REFRESH_FAILED",
                        f"{resource_title} refresh failed; returning cached {resource} data.",
                        error=str(exc),
                    )
                )
            if bool(cache_entry.get("stale")):
                warnings.insert(
                    0,
                    provider_warning(
                        "STALE_CACHE",
                        f"Returning stale {resource} cache because live refresh failed.",
                    ),
                )
            return _cached_load(refresh_attempted=True, warnings=warnings)
        return _empty_fail(
            refresh_attempted=True,
            warnings=(
                provider_warning(
                    "LIVE_REFRESH_FAILED",
                    f"{resource_title} refresh failed.",
                    error=str(exc),
                ),
            ),
        )

    fetched_at: str | None = None
    expires_at: str | None = None
    if fresh_timestamps is not None:
        fetched_at, expires_at = fresh_timestamps()
    return ProviderLoad(
        items=list(live_items),
        source=live_source,
        capability=live_capability,
        freshness_mode="live",
        cache_hit=False,
        stale=False,
        fetched_at=fetched_at,
        expires_at=expires_at,
        refresh_attempted=True,
        ok=True,
    )


def run_list_authenticated(
    auth: Any,  # AuthService
    *,
    paths: Any,  # KlmsPaths
    list_with_context: Callable[..., CommandResult],
    timeout_seconds: float = 10.0,
    **list_kwargs: Any,
) -> CommandResult:
    from .config import load_config
    from .session import build_session_bootstrap

    config = load_config(paths)

    def callback(context: BrowserContextLike, auth_mode: str, dashboard_state: dict[str, Any]) -> CommandResult:
        bootstrap = build_session_bootstrap(
            paths,
            context=context,
            config=config,
            auth_mode=auth_mode,
            timeout_seconds=timeout_seconds,
            dashboard_url=str(dashboard_state.get("final_url") or ""),
            dashboard_html=str(dashboard_state.get("html") or ""),
        )
        return list_with_context(
            context=context,
            config=config,
            auth_mode=auth_mode,
            bootstrap=bootstrap,
            **list_kwargs,
        )

    return auth.run_authenticated_with_state(
        config=config,
        headless=True,
        accept_downloads=False,
        timeout_seconds=timeout_seconds,
        callback=callback,
    )


def live_provider_load_from_result(
    result: CommandResult,
    *,
    deadline: RefreshDeadline | None,
    timeout_message: str = "Interactive refresh budget expired before the assignment refresh started.",
    empty_source: Source = "moodle_ajax",
    fetched_at: str | None = None,
) -> ProviderLoad:
    if deadline is not None and deadline.hard_expired():
        return ProviderLoad(
            items=[],
            source=empty_source,
            capability="degraded",
            freshness_mode="live",
            cache_hit=False,
            stale=False,
            fetched_at=None,
            expires_at=None,
            refresh_attempted=False,
            ok=False,
            warnings=(
                provider_warning("LIVE_REFRESH_TIMEOUT", timeout_message),
            ),
        )

    rows = [row for row in result.data if isinstance(row, dict)] if isinstance(result.data, list) else []
    return ProviderLoad(
        items=rows,
        source=result.source,
        capability=result.capability,
        freshness_mode="live",
        cache_hit=False,
        stale=False,
        fetched_at=fetched_at if fetched_at is not None else utc_now_iso(),
        expires_at=None,
        refresh_attempted=True,
        ok=True,
    )
