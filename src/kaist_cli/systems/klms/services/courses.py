from __future__ import annotations

from .... import klms as legacy


async def list_courses(*, include_all: bool = False, enrich: bool = True, limit: int | None = None) -> list[dict[str, object]]:
    return await legacy.klms_list_courses(include_all=include_all, enrich=enrich, limit=limit)


async def list_courses_api(*, include_all: bool = False, limit: int = 50) -> list[dict[str, object]]:
    return await legacy.klms_list_courses_api(include_all=include_all, limit=limit)


async def get_current_term() -> dict[str, object]:
    return await legacy.klms_get_current_term()


async def get_course(course_id: str) -> dict[str, object]:
    return await legacy.klms_get_course_info(course_id)
