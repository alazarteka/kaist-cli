from __future__ import annotations

import json
import sys
from datetime import datetime
from typing import Any

from ..core.terminal import sanitize_terminal_text


def is_tabular_list(data: Any) -> bool:
    return isinstance(data, list) and all(isinstance(item, dict) for item in data)


def _dataset_kind(rows: list[dict[str, Any]]) -> str:
    keys = {key for row in rows for key in row.keys()}
    if "due_iso" in keys:
        return "assignments"
    if "posted_iso" in keys:
        return "notices"
    if "viewer_url" in keys or "stream_url" in keys:
        return "videos"
    if "downloadable" in keys or "download_url" in keys:
        return "files"
    if "term_label" in keys:
        return "courses"
    if "kind" in keys and "time_iso" in keys:
        return "inbox"
    return "generic"


def table_columns(rows: list[dict[str, Any]]) -> list[str]:
    keys = {k for row in rows for k in row.keys()}
    kind = _dataset_kind(rows)
    preferred: dict[str, list[str]] = {
        "courses": ["id", "title", "course_code", "term_label", "professors"],
        "assignments": ["id", "title", "course_code", "due_iso"],
        "notices": ["id", "title", "posted_iso", "board_id"],
        "files": ["id", "title", "course_code", "kind", "downloadable"],
        "videos": ["id", "title", "course_code", "viewer_url"],
        "inbox": ["kind", "id", "title", "course_title", "time_iso"],
    }
    ordered = [key for key in preferred.get(kind, ["id", "title", "url"]) if key in keys]
    extra = sorted(key for key in keys if key not in ordered and key not in {"source", "auth_mode", "confidence", "course_code_base", "download_url"})
    return ordered + extra


def format_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return ", ".join(sanitize_terminal_text(item) for item in value)
    if isinstance(value, dict):
        return json.dumps(_sanitize_human_value(value), ensure_ascii=False)
    return sanitize_terminal_text(value)


def _sanitize_human_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {sanitize_terminal_text(key): _sanitize_human_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize_human_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_sanitize_human_value(item) for item in value)
    if isinstance(value, str):
        return sanitize_terminal_text(value)
    return value


def emit_table(rows: list[dict[str, Any]]) -> None:
    if not rows:
        print("(no rows)")
        return
    columns = table_columns(rows)
    widths: dict[str, int] = {}
    for col in columns:
        max_cell = max(len(format_cell(row.get(col))) for row in rows)
        widths[col] = min(max(len(col), max_cell), 72)

    header = " | ".join(sanitize_terminal_text(col).ljust(widths[col]) for col in columns)
    divider = "-+-".join("-" * widths[col] for col in columns)
    print(header)
    print(divider)
    for row in rows:
        parts: list[str] = []
        for col in columns:
            text = format_cell(row.get(col))
            if len(text) > widths[col]:
                text = text[: max(0, widths[col] - 3)] + "..."
            parts.append(text.ljust(widths[col]))
        print(" | ".join(parts))


def _parse_dt(value: str | None) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    normalized = text.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def _fmt_local(value: str | None) -> str:
    dt = _parse_dt(value)
    if dt is None:
        return sanitize_terminal_text(value or "").strip()
    return dt.astimezone().strftime("%a %b %-d %H:%M")


def _emit_today_summary(data: dict[str, Any]) -> bool:
    required = {"summary", "urgent_assignments", "recent_notices", "materials"}
    if not required.issubset(data.keys()):
        return False
    summary = data.get("summary") or {}
    urgent = data.get("urgent_assignments") or []
    notices = data.get("recent_notices") or []
    materials = data.get("materials") or []
    print(
        f"{sanitize_terminal_text(summary.get('urgent_assignment_count', len(urgent)))} urgent assignment(s), "
        f"{sanitize_terminal_text(summary.get('recent_notice_count', len(notices)))} recent notice(s), "
        f"{sanitize_terminal_text(summary.get('material_count', len(materials)))} material(s)."
    )
    if urgent:
        print("")
        print("Urgent assignments:")
        for row in urgent:
            course = sanitize_terminal_text(row.get("course_code") or row.get("course_title") or "").strip()
            due = _fmt_local(row.get("due_iso"))
            title = sanitize_terminal_text(row.get("title") or row.get("id") or "assignment")
            if course:
                print(f"- {title} — {course} — due {due}")
            else:
                print(f"- {title} — due {due}")
    if notices:
        print("")
        print("Recent notices:")
        for row in notices:
            posted = _fmt_local(row.get("posted_iso"))
            title = sanitize_terminal_text(row.get("title") or row.get("id") or "notice")
            print(f"- {title} — posted {posted}")
    if materials:
        print("")
        print("Materials:")
        for row in materials:
            course = sanitize_terminal_text(row.get("course_code") or row.get("course_title") or "").strip()
            title = sanitize_terminal_text(row.get("title") or row.get("id") or "material")
            if course:
                print(f"- {title} — {course}")
            else:
                print(f"- {title}")
    if data.get("warnings"):
        print("")
        print("Warnings:")
        for warning in data.get("warnings") or []:
            if isinstance(warning, dict):
                provider = sanitize_terminal_text(warning.get("provider") or "").strip()
                message = sanitize_terminal_text(warning.get("message") or warning.get("code") or "").strip()
                prefix = f"{provider}: " if provider else ""
                print(f"- {prefix}{message}")
    return True


def _emit_inbox_summary(data: dict[str, Any]) -> bool:
    items = data.get("items")
    providers = data.get("providers")
    if not isinstance(items, list) or providers is None:
        return False
    print(f"{len(items)} inbox item(s).")
    if items:
        print("")
        for idx, row in enumerate(items, start=1):
            if not isinstance(row, dict):
                print(f"{idx}. {format_cell(row)}")
                continue
            kind = sanitize_terminal_text(row.get("kind") or "item")
            title = sanitize_terminal_text(row.get("title") or row.get("id") or kind)
            course = sanitize_terminal_text(row.get("course_title") or row.get("course_code") or "").strip()
            when = _fmt_local(row.get("time_iso"))
            bits = [f"{idx}. [{kind}] {title}"]
            if course:
                bits.append(course)
            if when:
                bits.append(when)
            print(" — ".join(bits))
    if data.get("warnings"):
        print("")
        print("Warnings:")
        for warning in data.get("warnings") or []:
            if isinstance(warning, dict):
                provider = sanitize_terminal_text(warning.get("provider") or "").strip()
                message = sanitize_terminal_text(warning.get("message") or warning.get("code") or "").strip()
                prefix = f"{provider}: " if provider else ""
                print(f"- {prefix}{message}")
    return True


def _emit_sync_summary(data: dict[str, Any]) -> bool:
    providers = data.get("providers")
    if not isinstance(providers, dict):
        return False
    print("Sync summary:")
    for name in ("notice_board_ids", "notices", "files"):
        provider = providers.get(name)
        if not isinstance(provider, dict):
            continue
        status = sanitize_terminal_text(provider.get("status") or "unknown")
        item_count = provider.get("item_count")
        duration_ms = provider.get("duration_ms")
        detail: list[str] = []
        if item_count is not None:
            noun = "item" if int(item_count) == 1 else "items"
            detail.append(f"{int(item_count)} {noun}")
        if provider.get("source"):
            detail.append(sanitize_terminal_text(provider.get("source")))
        if provider.get("freshness_mode"):
            detail.append(sanitize_terminal_text(provider.get("freshness_mode")))
        if duration_ms is not None:
            detail.append(f"{int(duration_ms)}ms")
        elif provider.get("age_seconds") is not None:
            detail.append(f"age={int(float(provider['age_seconds']))}s")
        suffix = f" ({', '.join(detail)})" if detail else ""
        print(f"- {name}: {status}{suffix}")
    warnings = data.get("warnings") or []
    if warnings:
        print("")
        print("Warnings:")
        for warning in warnings:
            if isinstance(warning, dict):
                provider = sanitize_terminal_text(warning.get("provider") or "").strip()
                message = sanitize_terminal_text(warning.get("message") or warning.get("code") or "").strip()
                prefix = f"{provider}: " if provider else ""
                print(f"- {prefix}{message}")
    return True


def emit_text(data: Any, *, command_path: str | None = None) -> None:
    if isinstance(data, dict):
        if _emit_today_summary(data):
            return
        if _emit_inbox_summary(data):
            return
        if command_path == "klms sync run" and _emit_sync_summary(data):
            return
        for key, value in data.items():
            human_value = _sanitize_human_value(value)
            rendered = json.dumps(human_value, ensure_ascii=False) if isinstance(human_value, (dict, list)) else str(human_value)
            print(f"{sanitize_terminal_text(key)}: {rendered}")
        return
    if isinstance(data, list):
        if not data:
            print("(empty)")
            return
        for idx, item in enumerate(data, start=1):
            if isinstance(item, dict):
                title = sanitize_terminal_text(item.get("title") or item.get("id") or item.get("url") or f"item-{idx}")
                print(f"{idx}. {title}")
            else:
                print(f"{idx}. {sanitize_terminal_text(item)}")
        return
    print(sanitize_terminal_text(data))


def emit_json(data: Any, *, sort_keys: bool) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=sort_keys))


def emit_human_output(data: Any, output_format: str, *, command_path: str | None = None) -> None:
    resolved = output_format
    if output_format == "auto":
        if sys.stdout.isatty():
            resolved = "table" if is_tabular_list(data) else "text"
        else:
            resolved = "json"

    if resolved == "json":
        emit_json(data, sort_keys=False)
        return
    if resolved == "table":
        if is_tabular_list(data):
            emit_table(data)
        else:
            emit_text(data, command_path=command_path)
        return
    emit_text(data, command_path=command_path)
