from __future__ import annotations

from bs4 import BeautifulSoup  # type: ignore[import-untyped]

from typing import Any


def _legacy() -> Any:
    from .... import klms as legacy

    return legacy


def parse_notice_items(
    html: str,
    *,
    board_id: str,
    base_url: str,
    fallback_url_path: str,
) -> list[dict[str, object]]:
    soup = BeautifulSoup(html, "html.parser")
    return _legacy()._parse_notice_items_from_soup(  # type: ignore[attr-defined]
        soup,
        board_id=board_id,
        base_url=base_url,
        fallback_url_path=fallback_url_path,
    )


def parse_notice_detail(
    html: str,
    *,
    base_url: str,
    url: str | None = None,
    fallback_board_id: str | None = None,
    fallback_notice_id: str | None = None,
    include_html: bool = False,
) -> dict[str, object]:
    return _legacy()._parse_notice_detail_from_html(  # type: ignore[attr-defined]
        html,
        base_url=base_url,
        url=url,
        fallback_board_id=fallback_board_id,
        fallback_notice_id=fallback_notice_id,
        include_html=include_html,
    )
