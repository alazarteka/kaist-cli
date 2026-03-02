from __future__ import annotations

import json
import sys
from typing import Any


def is_tabular_list(data: Any) -> bool:
    return isinstance(data, list) and all(isinstance(item, dict) for item in data)


def table_columns(rows: list[dict[str, Any]]) -> list[str]:
    priority = [
        "id",
        "board_id",
        "course_id",
        "title",
        "due_iso",
        "posted_iso",
        "course_code_base",
        "course_code",
        "term_label",
        "kind",
        "url",
        "path",
        "source",
    ]
    keys = {k for row in rows for k in row.keys()}
    ordered = [k for k in priority if k in keys]
    extra = sorted(k for k in keys if k not in ordered)
    return ordered + extra


def format_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def emit_table(rows: list[dict[str, Any]]) -> None:
    if not rows:
        print("(no rows)")
        return
    columns = table_columns(rows)
    widths: dict[str, int] = {}
    for col in columns:
        max_cell = max(len(format_cell(row.get(col))) for row in rows)
        widths[col] = min(max(len(col), max_cell), 70)

    header = " | ".join(col.ljust(widths[col]) for col in columns)
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


def emit_text(data: Any) -> None:
    if isinstance(data, dict):
        for key, value in data.items():
            rendered = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
            print(f"{key}: {rendered}")
        return
    if isinstance(data, list):
        if not data:
            print("(empty)")
            return
        for idx, item in enumerate(data, start=1):
            if isinstance(item, dict):
                title = item.get("title") or item.get("id") or item.get("url") or f"item-{idx}"
                print(f"{idx}. {title}")
            else:
                print(f"{idx}. {item}")
        return
    print(str(data))


def emit_json(data: Any, *, sort_keys: bool) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=sort_keys))


def emit_human_output(data: Any, output_format: str) -> None:
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
            emit_text(data)
        return
    emit_text(data)
