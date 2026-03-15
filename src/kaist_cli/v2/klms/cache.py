from __future__ import annotations

import json
import threading
import time
from typing import Any

from .paths import KlmsPaths, ensure_private_dirs

_CACHE_LOCK = threading.RLock()


def _load_cache_payload(paths: KlmsPaths) -> dict[str, Any]:
    with _CACHE_LOCK:
        ensure_private_dirs(paths)
        try:
            payload = json.loads(paths.cache_path.read_text(encoding="utf-8"))
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}

        entries = payload.get("entries")
        if not isinstance(entries, dict):
            entries = {}

        normalized_entries: dict[str, Any] = {}
        dirty = False
        for key, entry in entries.items():
            if not isinstance(entry, dict):
                dirty = True
                continue
            expires_at = entry.get("expires_at")
            stored_at = entry.get("stored_at")
            if not isinstance(expires_at, (int, float)):
                dirty = True
                continue
            normalized_entries[str(key)] = {
                "stored_at": float(stored_at) if isinstance(stored_at, (int, float)) else None,
                "expires_at": float(expires_at),
                "value": entry.get("value"),
            }

        normalized = {"entries": normalized_entries}
        if dirty or payload.get("entries") != normalized_entries:
            paths.cache_path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return normalized


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
    with _CACHE_LOCK:
        payload = _load_cache_payload(paths)
        entries = payload.setdefault("entries", {})
        now = time.time()
        entries[str(key)] = {
            "stored_at": now,
            "expires_at": now + max(1, int(ttl_seconds)),
            "value": value,
        }
        paths.cache_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


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
    with _CACHE_LOCK:
        payload = _load_cache_payload(paths)
        entries = payload.get("entries") or {}
        kept: dict[str, Any] = {}
        removed = 0
        for key, entry in entries.items():
            key_text = str(key)
            if prefixes and any(key_text.startswith(prefix) for prefix in prefixes):
                removed += 1
                continue
            if not prefixes:
                removed += 1
                continue
            kept[key_text] = entry
        payload["entries"] = kept
        paths.cache_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return removed
