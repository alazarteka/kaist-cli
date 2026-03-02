from __future__ import annotations

from typing import Any


def _legacy() -> Any:
    from .... import klms as legacy

    return legacy


async def list_files(*, course_id: str | None = None, limit: int | None = None) -> list[dict[str, object]]:
    return await _legacy().klms_list_files(course_id=course_id, limit=limit)
