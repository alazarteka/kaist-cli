from __future__ import annotations

import argparse
from datetime import datetime, timezone
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _sanitize_schema_part(value: str | None) -> str:
    return (value or "").replace("-", "_").strip("_")


def schema_for_args(args: argparse.Namespace) -> str:
    schema_name = getattr(args, "schema_name", None)
    if isinstance(schema_name, str) and schema_name.strip():
        return schema_name

    system = str(getattr(args, "system", "")).strip()
    if system != "klms":
        return "kaist.cli.generic.v1"

    group = _sanitize_schema_part(getattr(args, "group", "unknown")) or "unknown"
    action = _sanitize_schema_part(getattr(args, "action", None))

    if group in {"config", "auth", "dev"} and action:
        return f"kaist.klms.{group}.{action}.v1"

    resource = _sanitize_schema_part(getattr(args, "resource", None))
    if group == "list" and resource:
        return f"kaist.klms.{resource}.v1"

    if group == "get" and resource == "file":
        return "kaist.klms.download.v1"

    if group == "sync":
        sync_action = _sanitize_schema_part(getattr(args, "sync_action", None))
        if sync_action == "run" or not sync_action:
            return "kaist.klms.sync.v1"
        return f"kaist.klms.sync.{sync_action}.v1"

    return f"kaist.klms.{group}.v1"


def infer_source(data: Any) -> str:
    if isinstance(data, dict):
        src = data.get("source")
        if isinstance(src, str) and src.strip():
            return src
        if isinstance(data.get("recommended_endpoints"), list):
            return "api"
        if isinstance(data.get("auth_mode"), str):
            return "html"
        return "mixed"

    if isinstance(data, list):
        sources = {
            str(item.get("source")).strip()
            for item in data
            if isinstance(item, dict) and isinstance(item.get("source"), str) and str(item.get("source")).strip()
        }
        if not sources:
            return "mixed"
        if len(sources) == 1:
            return next(iter(sources))
        return "mixed"

    return "mixed"


def explicit_source(args: argparse.Namespace) -> str | None:
    value = getattr(args, "_explicit_source", None)
    if isinstance(value, str) and value.strip():
        return value
    return None


def explicit_capability(args: argparse.Namespace) -> str | None:
    value = getattr(args, "_explicit_capability", None)
    if isinstance(value, str) and value.strip():
        return value
    return None


def extract_cursor_fields(data: Any) -> tuple[str | None, str | None]:
    if not isinstance(data, dict):
        return None, None
    cur = data.get("cursor")
    nxt = data.get("next_cursor")
    return (str(cur) if isinstance(cur, str) else None, str(nxt) if isinstance(nxt, str) else None)


def command_label(args: argparse.Namespace) -> str:
    command_path = getattr(args, "command_path", None)
    if isinstance(command_path, str) and command_path.strip():
        return command_path

    parts = [
        str(getattr(args, "system", "")),
        str(getattr(args, "group", "")),
        str(getattr(args, "action", "")),
        str(getattr(args, "resource", "")),
        str(getattr(args, "sync_action", "")),
    ]
    return " ".join(p for p in parts if p and p != "None")


def success_envelope(args: argparse.Namespace, data: Any) -> dict[str, Any]:
    cursor, next_cursor = extract_cursor_fields(data)
    meta = {
        "source": explicit_source(args) or infer_source(data),
        "cursor": cursor,
        "next_cursor": next_cursor,
        "command": command_label(args),
    }
    capability = explicit_capability(args)
    if capability is not None:
        meta["capability"] = capability
    return {
        "schema": schema_for_args(args),
        "ok": True,
        "generated_at": utc_now_iso(),
        "meta": meta,
        "data": data,
    }


def error_envelope(
    args: argparse.Namespace,
    *,
    code: str,
    message: str,
    retryable: bool,
    hint: str | None,
) -> dict[str, Any]:
    return {
        "schema": schema_for_args(args),
        "ok": False,
        "generated_at": utc_now_iso(),
        "error": {
            "code": code,
            "message": message,
            "retryable": retryable,
            "hint": hint,
        },
    }
