from __future__ import annotations

from typing import Any


def _legacy() -> Any:
    from .... import klms as legacy

    return legacy


async def list_notices(
    *,
    notice_board_id: str | None = None,
    max_pages: int = 1,
    stop_post_id: str | None = None,
    since_iso: str | None = None,
    limit: int | None = None,
) -> list[dict[str, object]]:
    return await _legacy().klms_list_notices(
        notice_board_id=notice_board_id,
        max_pages=max_pages,
        stop_post_id=stop_post_id,
        since_iso=since_iso,
        limit=limit,
    )


async def get_notice(
    notice_id: str,
    *,
    notice_board_id: str | None = None,
    max_pages: int = 3,
    include_html: bool = False,
) -> dict[str, object]:
    return await _legacy().klms_get_notice(
        notice_id,
        notice_board_id=notice_board_id,
        max_pages=max_pages,
        include_html=include_html,
    )
