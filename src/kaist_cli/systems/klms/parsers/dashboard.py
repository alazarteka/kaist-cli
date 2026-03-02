from __future__ import annotations

from bs4 import BeautifulSoup  # type: ignore[import-untyped]

from typing import Any


def _legacy() -> Any:
    from .... import klms as legacy

    return legacy


def parse_courses_from_dashboard(html: str, *, base_url: str) -> list[dict[str, object]]:
    return _legacy()._discover_courses_from_dashboard(html, base_url)  # type: ignore[attr-defined]


def parse_term_from_dashboard(html: str) -> dict[str, object] | None:
    return _legacy()._extract_current_term_from_dashboard(html)  # type: ignore[attr-defined]


def parse_notice_board_ids_from_course_html(html: str) -> list[dict[str, str]]:
    return _legacy()._discover_notice_board_ids_from_course_page(html)  # type: ignore[attr-defined]


def parse_pagination_pages(html: str) -> list[int]:
    soup = BeautifulSoup(html, "html.parser")
    return _legacy()._extract_pagination_pages(soup)  # type: ignore[attr-defined]
