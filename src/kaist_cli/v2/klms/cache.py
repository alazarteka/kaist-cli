from __future__ import annotations

import threading
import time
from typing import Any, Callable

from ...core.state_store import file_lock, read_json_file, write_json_file_atomic
from .paths import KlmsPaths, ensure_private_dirs

CACHE_VERSION = 1
_CACHE_LOCK = threading.RLock()
_CACHE_SNAPSHOT: dict[str, Any] | None = None
_CACHE_SNAPSHOT_PATH: str | None = None
_CACHE_SNAPSHOT_FINGERPRINT: tuple[int, int] | None = None


def _cache_document_is_supported(payload: dict[str, Any]) -> bool:
    if "version" not in payload:
        return True
    version = payload.get("version")
    return type(version) is int and version == CACHE_VERSION


def _normalize_cache_payload(payload: dict[str, Any]) -> dict[str, Any]:
    entries = payload.get("entries")
    if not isinstance(entries, dict):
        entries = {}

    normalized_entries: dict[str, Any] = {}
    for key, entry in entries.items():
        if not isinstance(entry, dict):
            continue
        expires_at = entry.get("expires_at")
        stored_at = entry.get("stored_at")
        if not isinstance(expires_at, (int, float)):
            continue
        normalized_entries[str(key)] = {
            "stored_at": float(stored_at) if isinstance(stored_at, (int, float)) else None,
            "expires_at": float(expires_at),
            "value": entry.get("value"),
        }
    return {"entries": normalized_entries}


def _clone_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {"entries": dict(payload.get("entries") or {})}


def _cache_fingerprint(paths: KlmsPaths) -> tuple[int, int] | None:
    try:
        stat = paths.cache_path.stat()
    except FileNotFoundError:
        return None
    return (int(stat.st_mtime_ns), int(stat.st_size))


def _set_snapshot(paths: KlmsPaths, payload: dict[str, Any]) -> None:
    global _CACHE_SNAPSHOT, _CACHE_SNAPSHOT_PATH, _CACHE_SNAPSHOT_FINGERPRINT
    _CACHE_SNAPSHOT = _clone_payload(payload)
    _CACHE_SNAPSHOT_PATH = str(paths.cache_path)
    _CACHE_SNAPSHOT_FINGERPRINT = _cache_fingerprint(paths)


def _load_cache_payload(paths: KlmsPaths) -> dict[str, Any]:
    with _CACHE_LOCK:
        path_key = str(paths.cache_path)
        fingerprint = _cache_fingerprint(paths)
        if (
            _CACHE_SNAPSHOT is not None
            and _CACHE_SNAPSHOT_PATH == path_key
            and _CACHE_SNAPSHOT_FINGERPRINT == fingerprint
        ):
            return _clone_payload(_CACHE_SNAPSHOT)

        payload = read_json_file(paths.cache_path, default={})
        if not _cache_document_is_supported(payload):
            return {"entries": {}}
        normalized = _normalize_cache_payload(payload)
        _set_snapshot(paths, normalized)
        return _clone_payload(normalized)


def _update_cache_entries(paths: KlmsPaths, *, updater: Callable[[dict[str, Any]], None]) -> None:
    with _CACHE_LOCK:
        ensure_private_dirs(paths)
        lock_path = paths.cache_path.with_suffix(paths.cache_path.suffix + ".lock")
        with file_lock(lock_path):
            payload = read_json_file(paths.cache_path, default={})
            if not _cache_document_is_supported(payload):
                return
            normalized = _normalize_cache_payload(payload)
            entries = normalized["entries"]
            updater(entries)
            stored = {"version": CACHE_VERSION, "entries": entries}
            write_json_file_atomic(paths.cache_path, stored, chmod_mode=0o600)
            _set_snapshot(paths, stored)


def _entry_status(entry: dict[str, Any]) -> dict[str, Any]:
    now = time.time()
    expires_at = float(entry.get("expires_at") or 0.0)
    stored_at = entry.get("stored_at")
    stored_at_value = float(stored_at) if isinstance(stored_at, (int, float)) else None
    return {
        "stored_at": stored_at_value,
        "expires_at": expires_at,
        "stale": expires_at <= now,
        "age_seconds": max(0.0, now - stored_at_value) if stored_at_value is not None else None,
        "ttl_remaining_seconds": max(0.0, expires_at - now),
        "value": entry.get("value"),
    }


def load_cache_entry(paths: KlmsPaths, key: str) -> dict[str, Any] | None:
    entries = _load_cache_payload(paths).get("entries") or {}
    entry = entries.get(str(key))
    if isinstance(entry, dict):
        return _entry_status(entry)
    return None


def load_cache_value(paths: KlmsPaths, key: str, *, allow_stale: bool = False) -> Any | None:
    entry = load_cache_entry(paths, key)
    if entry is not None and (allow_stale or not bool(entry.get("stale"))):
        return entry.get("value")
    return None


def save_cache_value(paths: KlmsPaths, key: str, value: Any, *, ttl_seconds: int) -> None:
    def updater(entries: dict[str, Any]) -> None:
        now = time.time()
        entries[str(key)] = {
            "stored_at": now,
            "expires_at": now + max(1, int(ttl_seconds)),
            "value": value,
        }
    _update_cache_entries(paths, updater=updater)


def list_cache_entries(paths: KlmsPaths, *, prefixes: tuple[str, ...] = ()) -> dict[str, dict[str, Any]]:
    entries = _load_cache_payload(paths).get("entries") or {}
    out: dict[str, dict[str, Any]] = {}
    for key, entry in entries.items():
        key_text = str(key)
        if prefixes and not any(key_text.startswith(prefix) for prefix in prefixes):
            continue
        if isinstance(entry, dict):
            out[key_text] = _entry_status(entry)
    return out


def clear_cache_entries(paths: KlmsPaths, *, prefixes: tuple[str, ...] = ()) -> int:
    removed = 0

    def updater(entries: dict[str, Any]) -> None:
        nonlocal removed
        kept: dict[str, Any] = {}
        for key, entry in entries.items():
            key_text = str(key)
            if prefixes and any(key_text.startswith(prefix) for prefix in prefixes):
                removed += 1
                continue
            if not prefixes:
                removed += 1
                continue
            kept[key_text] = entry
        entries.clear()
        entries.update(kept)

    _update_cache_entries(paths, updater=updater)
    return removed
