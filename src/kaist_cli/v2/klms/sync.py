from __future__ import annotations

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


def _entry_brief(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "stale": bool(entry.get("stale")),
        "stored_at": entry.get("stored_at"),
        "expires_at": entry.get("expires_at"),
        "age_seconds": entry.get("age_seconds"),
        "ttl_remaining_seconds": entry.get("ttl_remaining_seconds"),
    }


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
        groups[group].append(
            {
                "key": key,
                **_entry_brief(entry),
            }
        )

    providers: dict[str, Any] = {}
    for group, items in groups.items():
        items.sort(key=lambda item: float(item.get("stored_at") or 0.0), reverse=True)
        latest = items[0] if items else None
        providers[group] = {
            "entry_count": len(items),
            "has_fresh": any(not bool(item.get("stale")) for item in items),
            "latest": latest,
            "entries": items,
        }
    return {"providers": providers}


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
            notices = self._notices.refresh_cache_with_context(
                context=context,
                config=config,
                auth_mode=auth_mode,
                max_pages=1,
                bootstrap=bootstrap,
            )
            files = self._files.refresh_cache_with_context(
                context=context,
                config=config,
                auth_mode=auth_mode,
                bootstrap=bootstrap,
            )
            warnings = notices.provider_warnings("notices") + files.provider_warnings("files")
            payload = _status_from_entries(list_cache_entries(self._paths, prefixes=SYNC_CACHE_PREFIXES))
            payload["providers"]["notices"].update(notices.provider_status())
            payload["providers"]["files"].update(files.provider_status())
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
