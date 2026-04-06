from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from ...core.state_store import read_json_file, update_json_file, write_json_file_atomic
from .paths import KlmsPaths

AUTH_SESSION_VERSION = 1


def _default_session_payload() -> dict[str, Any]:
    return {"version": AUTH_SESSION_VERSION}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def utc_now_iso() -> str:
    return _utc_now().isoformat().replace("+00:00", "Z")


def session_expiry_iso(*, ttl_seconds: float) -> str:
    dt = datetime.fromtimestamp(_utc_now().timestamp() + max(float(ttl_seconds), 1.0), tz=timezone.utc).replace(microsecond=0)
    return dt.isoformat().replace("+00:00", "Z")


def new_auth_session_id() -> str:
    return uuid4().hex[:12]


def load_auth_session(paths: KlmsPaths) -> dict[str, Any] | None:
    payload = read_json_file(paths.auth_session_path, default=_default_session_payload())
    if int(payload.get("version") or 0) != AUTH_SESSION_VERSION:
        return None
    session_id = str(payload.get("session_id") or "").strip()
    if not session_id:
        return None
    return payload


def save_auth_session(paths: KlmsPaths, payload: dict[str, Any]) -> dict[str, Any]:
    updated = dict(payload)
    updated["version"] = AUTH_SESSION_VERSION
    write_json_file_atomic(paths.auth_session_path, updated, chmod_mode=0o600)
    return updated


def clear_auth_session(paths: KlmsPaths) -> None:
    write_json_file_atomic(paths.auth_session_path, _default_session_payload(), chmod_mode=0o600)


def update_auth_session(paths: KlmsPaths, *, updater: Any) -> dict[str, Any]:
    return update_json_file(
        paths.auth_session_path,
        default=_default_session_payload(),
        updater=updater,
        chmod_mode=0o600,
    )
