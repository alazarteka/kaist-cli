from __future__ import annotations

from bs4 import BeautifulSoup  # type: ignore[import-untyped]

from .... import klms as legacy


def parse_courses_from_dashboard(html: str, *, base_url: str) -> list[dict[str, object]]:
    return legacy._discover_courses_from_dashboard(html, base_url)  # type: ignore[attr-defined]


def parse_term_from_dashboard(html: str) -> dict[str, object] | None:
    return legacy._extract_current_term_from_dashboard(html)  # type: ignore[attr-defined]


def parse_notice_board_ids_from_course_html(html: str) -> list[dict[str, str]]:
    return legacy._discover_notice_board_ids_from_course_page(html)  # type: ignore[attr-defined]


def parse_pagination_pages(html: str) -> list[int]:
    soup = BeautifulSoup(html, "html.parser")
    return legacy._extract_pagination_pages(soup)  # type: ignore[attr-defined]
