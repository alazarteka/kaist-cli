from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from ...core.state_store import read_json_file, update_json_file
from .models import FileItem, Video
from .paths import KlmsPaths

MEDIA_RECENCY_STORE_VERSION = 1


def _default_store() -> dict[str, Any]:
    return {"version": MEDIA_RECENCY_STORE_VERSION, "files": {}, "videos": {}}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _bucket(store: dict[str, Any], key: str) -> dict[str, dict[str, Any]]:
    raw = store.get(key)
    if not isinstance(raw, dict):
        raw = {}
    return {str(name): value for name, value in raw.items() if isinstance(name, str) and isinstance(value, dict)}


def _seen_payload(existing: dict[str, Any] | None, *, observed_at: str) -> dict[str, str]:
    return {
        "first_seen_at": str((existing or {}).get("first_seen_at") or observed_at),
        "last_seen_at": observed_at,
    }


def _file_key(item: FileItem) -> str | None:
    target = str(item.id or item.download_url or item.url or "").strip()
    if not target:
        return None
    course_id = str(item.course_id or "").strip() or "-"
    return f"{course_id}:{target}"


def _video_key(item: Video) -> str | None:
    target = str(item.id or item.url or item.viewer_url or "").strip()
    if not target:
        return None
    course_id = str(item.course_id or "").strip() or "-"
    return f"{course_id}:{target}"


def _apply_file_records(items: list[FileItem], records: dict[str, dict[str, Any]]) -> list[FileItem]:
    out: list[FileItem] = []
    for item in items:
        record = records.get(_file_key(item) or "")
        if record:
            out.append(
                replace(
                    item,
                    first_seen_at=str(record.get("first_seen_at") or "").strip() or item.first_seen_at,
                    last_seen_at=str(record.get("last_seen_at") or "").strip() or item.last_seen_at,
                )
            )
        else:
            out.append(item)
    return out


def _apply_video_records(items: list[Video], records: dict[str, dict[str, Any]]) -> list[Video]:
    out: list[Video] = []
    for item in items:
        record = records.get(_video_key(item) or "")
        if record:
            out.append(
                replace(
                    item,
                    first_seen_at=str(record.get("first_seen_at") or "").strip() or item.first_seen_at,
                    last_seen_at=str(record.get("last_seen_at") or "").strip() or item.last_seen_at,
                )
            )
        else:
            out.append(item)
    return out


def load_media_recency(paths: KlmsPaths) -> dict[str, Any]:
    store = read_json_file(paths.media_recency_store_path, default=_default_store())
    if int(store.get("version") or 0) != MEDIA_RECENCY_STORE_VERSION:
        return _default_store()
    return {
        "version": MEDIA_RECENCY_STORE_VERSION,
        "files": _bucket(store, "files"),
        "videos": _bucket(store, "videos"),
    }


def enrich_files_with_recency(paths: KlmsPaths, items: list[FileItem]) -> list[FileItem]:
    return _apply_file_records(items, _bucket(load_media_recency(paths), "files"))


def enrich_videos_with_recency(paths: KlmsPaths, items: list[Video]) -> list[Video]:
    return _apply_video_records(items, _bucket(load_media_recency(paths), "videos"))


def observe_files(paths: KlmsPaths, items: list[FileItem], *, observed_at: str | None = None) -> list[FileItem]:
    seen_at = observed_at or _utc_now_iso()

    def updater(current: dict[str, Any]) -> dict[str, Any]:
        files = _bucket(current, "files")
        for item in items:
            key = _file_key(item)
            if not key:
                continue
            files[key] = _seen_payload(files.get(key), observed_at=seen_at)
        return {
            "version": MEDIA_RECENCY_STORE_VERSION,
            "files": files,
            "videos": _bucket(current, "videos"),
        }

    updated = update_json_file(
        paths.media_recency_store_path,
        default=_default_store(),
        updater=updater,
        chmod_mode=0o600,
    )
    return _apply_file_records(items, _bucket(updated, "files"))


def observe_videos(paths: KlmsPaths, items: list[Video], *, observed_at: str | None = None) -> list[Video]:
    seen_at = observed_at or _utc_now_iso()

    def updater(current: dict[str, Any]) -> dict[str, Any]:
        videos = _bucket(current, "videos")
        for item in items:
            key = _video_key(item)
            if not key:
                continue
            videos[key] = _seen_payload(videos.get(key), observed_at=seen_at)
        return {
            "version": MEDIA_RECENCY_STORE_VERSION,
            "files": _bucket(current, "files"),
            "videos": videos,
        }

    updated = update_json_file(
        paths.media_recency_store_path,
        default=_default_store(),
        updater=updater,
        chmod_mode=0o600,
    )
    return _apply_video_records(items, _bucket(updated, "videos"))
