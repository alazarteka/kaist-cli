from __future__ import annotations

from .... import klms as legacy


async def list_inbox(*, limit: int = 30, max_notice_pages: int = 1, since_iso: str | None = None) -> list[dict[str, object]]:
    return await legacy.klms_inbox(limit=limit, max_notice_pages=max_notice_pages, since_iso=since_iso)
