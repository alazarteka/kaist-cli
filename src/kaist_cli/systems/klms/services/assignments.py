from __future__ import annotations

from .... import klms as legacy


async def list_assignments(
    *,
    course_id: str | None = None,
    since_iso: str | None = None,
    limit: int | None = None,
) -> list[dict[str, object]]:
    return await legacy.klms_list_assignments(course_id=course_id, since_iso=since_iso, limit=limit)


async def get_assignment(assignment_id: str, *, course_id: str | None = None) -> dict[str, object]:
    rows = await list_assignments(course_id=course_id, limit=None)
    target_id = str(assignment_id).strip()
    for row in rows:
        if str(row.get("id") or "") == target_id:
            return row
    raise FileNotFoundError(f"Assignment not found: {assignment_id}")
