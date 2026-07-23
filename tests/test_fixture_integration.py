from __future__ import annotations

from pathlib import Path

from bs4 import BeautifulSoup  # type: ignore[import-untyped]

from cli_helpers import FIXTURES
from kaist_cli.v2.klms.courses import _discover_courses_from_dashboard
from kaist_cli.v2.klms.notices import _parse_notice_items_from_soup


def test_fixture_integration_parses_dashboard_courses_and_notice_titles() -> None:
    dashboard_html = (FIXTURES / "dashboard_sample.html").read_text(encoding="utf-8")
    notices_html = (FIXTURES / "notices_sample.html").read_text(encoding="utf-8")

    courses = _discover_courses_from_dashboard(dashboard_html, base_url="https://klms.kaist.ac.kr")
    notices = _parse_notice_items_from_soup(
        BeautifulSoup(notices_html, "html.parser"),
        board_id="1174096",
        base_url="https://klms.kaist.ac.kr",
        fallback_url_path="/mod/courseboard/view.php?id=1174096",
    )

    assert courses
    assert all(course.id for course in courses)

    assert notices
    assert all(notice.title for notice in notices)
