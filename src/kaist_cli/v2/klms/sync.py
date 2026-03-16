from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from ..contracts import CommandResult
from .auth import AuthService
from .cache import clear_cache_entries, list_cache_entries
from .config import load_config
from .files import FileService
from .notices import NoticeService
from .paths import KlmsPaths
from .session import build_session_bootstrap

SYNC_CACHE_PREFIXES = (
    "notice-board-ids::",
    "notice-board-map-v2::",
    "notice-list::",
    "notice-list-v2::",
    "file-list::",
    "file-content-api-status::",
)


def _entry_item_count(entry: dict[str, Any]) -> int | None:
    value = entry.get("value")
    if isinstance(value, list):
        return len(value)
    if isinstance(value, dict):
        return len(value)
    return None


def _epoch_to_iso(value: Any) -> str | None:
    if not isinstance(value, (int, float)):
        return None
    return datetime.fromtimestamp(float(value), tz=timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _cache_group_name(key: str) -> str:
    if key.startswith("notice-board-ids::"):
        return "notice_board_ids"
    if key.startswith("notice-board-map-v2::"):
        return "notice_board_ids"
    if key.startswith("notice-list::"):
        return "notices"
    if key.startswith("notice-list-v2::"):
        return "notices"
    if key.startswith("file-list::"):
        return "files"
    if key.startswith("file-content-api-status::"):
        return "files"
    return "other"


def _status_from_entries(entries: dict[str, dict[str, Any]]) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = {
        "notice_board_ids": [],
        "notices": [],
        "files": [],
    }
    for key, entry in entries.items():
        group = _cache_group_name(key)
        if group not in groups:
            continue
        groups[group].append({"key": key, **entry})

    providers: dict[str, Any] = {}
    for group, items in groups.items():
        items.sort(key=lambda item: float(item.get("stored_at") or 0.0), reverse=True)
        latest = items[0] if items else None
        if latest is None:
            providers[group] = {
                "provider": group,
                "status": "skipped",
                "item_count": 0,
                "duration_ms": None,
                "warnings": [],
                "fetched_at": None,
                "expires_at": None,
                "cache_hit": False,
                "stale": False,
                "entry_count": 0,
            }
            continue
        stale = bool(latest.get("stale"))
        providers[group] = {
            "provider": group,
            "status": "cache_hit" if not stale else "stale",
            "item_count": _entry_item_count(latest),
            "duration_ms": None,
            "warnings": [],
            "fetched_at": _epoch_to_iso(latest.get("stored_at")),
            "expires_at": _epoch_to_iso(latest.get("expires_at")),
            "cache_hit": not stale,
            "stale": stale,
            "entry_count": len(items),
            "age_seconds": latest.get("age_seconds"),
            "ttl_remaining_seconds": latest.get("ttl_remaining_seconds"),
        }
    return {"providers": providers}


def _provider_summary(name: str, payload: dict[str, Any], *, duration_ms: int) -> dict[str, Any]:
    warnings = [dict(warning) for warning in payload.get("warnings") or [] if isinstance(warning, dict)]
    return {
        "provider": name,
        "status": str(payload.get("status") or ("failed" if not payload.get("ok") else "refreshed")),
        "item_count": payload.get("item_count", payload.get("count")),
        "duration_ms": duration_ms,
        "warnings": warnings,
        "fetched_at": payload.get("fetched_at"),
        "expires_at": payload.get("expires_at"),
        "cache_hit": bool(payload.get("cache_hit")),
        "stale": bool(payload.get("stale")),
        "source": payload.get("source"),
        "capability": payload.get("capability"),
        "refresh_attempted": bool(payload.get("refresh_attempted")),
        "ok": bool(payload.get("ok", True)),
    }


class SyncService:
    def __init__(
        self,
        paths: KlmsPaths,
        auth: AuthService,
        notices: NoticeService,
        files: FileService,
    ) -> None:
        self._paths = paths
        self._auth = auth
        self._notices = notices
        self._files = files

    def run(self) -> CommandResult:
        config = load_config(self._paths)

        def callback(context: Any, auth_mode: str, dashboard_state: dict[str, Any]) -> CommandResult:
            bootstrap = build_session_bootstrap(
                self._paths,
                context=context,
                config=config,
                auth_mode=auth_mode,
                timeout_seconds=15.0,
                dashboard_url=str(dashboard_state.get("final_url") or ""),
                dashboard_html=str(dashboard_state.get("html") or ""),
            )
            notices_started = time.perf_counter()
            notices = self._notices.refresh_cache_with_context(
                context=context,
                config=config,
                auth_mode=auth_mode,
                max_pages=1,
                bootstrap=bootstrap,
            )
            notices_duration_ms = int((time.perf_counter() - notices_started) * 1000)
            files_started = time.perf_counter()
            files = self._files.refresh_cache_with_context(
                context=context,
                config=config,
                auth_mode=auth_mode,
                bootstrap=bootstrap,
            )
            files_duration_ms = int((time.perf_counter() - files_started) * 1000)
            warnings = notices.provider_warnings("notices") + files.provider_warnings("files")
            payload = _status_from_entries(list_cache_entries(self._paths, prefixes=SYNC_CACHE_PREFIXES))
            payload["providers"]["notices"] = _provider_summary("notices", notices.provider_status(), duration_ms=notices_duration_ms)
            payload["providers"]["files"] = _provider_summary("files", files.provider_status(), duration_ms=files_duration_ms)
            payload["warnings"] = warnings
            return CommandResult(
                data=payload,
                source="mixed",
                capability="degraded" if warnings else "partial",
            )

        return self._auth.run_authenticated_with_state(
            config=config,
            headless=True,
            accept_downloads=False,
            timeout_seconds=20.0,
            callback=callback,
        )

    def status(self) -> CommandResult:
        payload = _status_from_entries(list_cache_entries(self._paths, prefixes=SYNC_CACHE_PREFIXES))
        return CommandResult(data=payload, source="cache", capability="partial")

    def reset(self) -> CommandResult:
        removed = clear_cache_entries(self._paths, prefixes=SYNC_CACHE_PREFIXES)
        payload = _status_from_entries(list_cache_entries(self._paths, prefixes=SYNC_CACHE_PREFIXES))
        payload["removed_entries"] = removed
        return CommandResult(data=payload, source="cache", capability="partial")
