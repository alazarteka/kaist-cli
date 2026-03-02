from __future__ import annotations

import asyncio
from typing import Any

from ... import klms as legacy


def set_config(
    base_url: str | None = None,
    *,
    dashboard_path: str | None = None,
    course_ids: list[str] | None = None,
    notice_board_ids: list[str] | None = None,
    exclude_course_title_patterns: list[str] | None = None,
    merge_existing: bool = True,
) -> dict[str, Any]:
    return legacy.klms_configure(
        base_url,
        dashboard_path=dashboard_path,
        course_ids=course_ids,
        notice_board_ids=notice_board_ids,
        exclude_course_title_patterns=exclude_course_title_patterns,
        merge_existing=merge_existing,
    )


def show_config() -> dict[str, Any]:
    return asyncio.run(legacy.klms_status(validate=False))
