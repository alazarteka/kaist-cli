from __future__ import annotations

from typing import Any


def _legacy() -> Any:
    from .... import klms as legacy

    return legacy


async def discover(*, max_courses: int = 2, max_notice_boards: int = 2) -> dict[str, object]:
    return await _legacy().klms_discover_api(max_courses=max_courses, max_notice_boards=max_notice_boards)


def map_discovered(*, report_path: str | None = None) -> dict[str, object]:
    return _legacy().klms_map_api(report_path=report_path)
