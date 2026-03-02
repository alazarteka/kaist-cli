from __future__ import annotations

from pathlib import Path

from kaist_cli.systems.klms.parsers import assignments, dashboard, files, notices


FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_dashboard_parser_fixture() -> None:
    html = (FIXTURES / "dashboard_sample.html").read_text(encoding="utf-8")
    rows = dashboard.parse_courses_from_dashboard(html, base_url="https://klms.kaist.ac.kr")
    assert len(rows) == 2
    assert rows[0]["id"] == "180871"

    term = dashboard.parse_term_from_dashboard(html)
    assert term is not None
    assert term["term_label"] == "2026 Spring"


def test_notice_parser_fixture() -> None:
    html = (FIXTURES / "notices_sample.html").read_text(encoding="utf-8")
    rows = notices.parse_notice_items(
        html,
        board_id="1174096",
        base_url="https://klms.kaist.ac.kr",
        fallback_url_path="/mod/courseboard/view.php?id=1174096",
    )
    assert len(rows) == 1
    assert rows[0]["id"] == "9001"
    assert rows[0]["title"] == "Exam Venue Update"

    pages = dashboard.parse_pagination_pages(html)
    assert pages == [0, 2]


def test_notice_detail_parser_fixture() -> None:
    html = (FIXTURES / "notice_detail_sample.html").read_text(encoding="utf-8")
    detail = notices.parse_notice_detail(
        html,
        base_url="https://klms.kaist.ac.kr",
        url="https://klms.kaist.ac.kr/mod/courseboard/article.php?id=1174096&bwid=9001",
        include_html=True,
    )
    assert detail["id"] == "9001"
    assert detail["board_id"] == "1174096"
    assert detail["title"] == "Exam Venue Update"
    assert detail["author"] == "Prof. Kim"
    assert detail["posted_iso"] is not None
    assert "room 1201" in str(detail["body_text"])
    attachments = detail["attachments"]
    assert isinstance(attachments, list)
    assert len(attachments) == 1
    assert attachments[0]["filename"] == "exam-room-map.pdf"
    assert attachments[0]["is_video"] is False
    assert "body_html" in detail


def test_assignment_api_shape_parser_fixture() -> None:
    payload = {
        "events": [
            {
                "id": 500,
                "instance": 700,
                "courseid": 180871,
                "name": "HW1",
                "modulename": "assign",
                "url": "/mod/assign/view.php?id=700",
                "timesort": 1771200000,
            }
        ]
    }
    rows = assignments.extract_assignment_rows_from_calendar_data(payload, base_url="https://klms.kaist.ac.kr")
    assert len(rows) == 1
    assert rows[0]["id"] == "700"
    assert rows[0]["course_id"] == "180871"


def test_file_parser_helpers() -> None:
    assert files.is_video_filename("lecture.mp4") is True
    assert files.is_video_url("https://cdn.example.com/stream/hls") is True
    assert files.material_kind_from_module("resource") == "file"
