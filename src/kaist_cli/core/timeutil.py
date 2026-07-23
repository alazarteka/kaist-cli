from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def iso_from_epoch_seconds(value: float | int | None) -> str | None:
    if not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    except Exception:
        return None


def cache_is_fresh_enough(entry: dict[str, Any] | None, *, max_age_seconds: int = 3600) -> bool:
    if not isinstance(entry, dict):
        return False
    age_seconds = entry.get("age_seconds")
    if isinstance(age_seconds, (int, float)):
        return float(age_seconds) <= float(max_age_seconds)
    stored_at = entry.get("stored_at")
    if isinstance(stored_at, (int, float)):
        return (datetime.now(timezone.utc).timestamp() - float(stored_at)) <= float(max_age_seconds)
    return False
