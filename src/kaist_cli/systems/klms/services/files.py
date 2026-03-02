from __future__ import annotations

from .... import klms as legacy


async def list_files(*, course_id: str | None = None, limit: int | None = None) -> list[dict[str, object]]:
    return await legacy.klms_list_files(course_id=course_id, limit=limit)
