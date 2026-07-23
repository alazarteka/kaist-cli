from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import fcntl
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from bs4 import BeautifulSoup  # type: ignore[import-untyped]

from cli_helpers import (
    FIXTURES,
    ROOT,
    _FakeSecretStore,
    _read_fixture,
    _write_config,
    _write_storage_state,
    run_cli,
)
from kaist_cli.v2.klms import auth as auth_module
from kaist_cli.v2.klms import auth_browser as auth_browser_module
from kaist_cli.v2.klms import auth_otp as auth_otp_module
from kaist_cli.v2.klms import auth_sso as auth_sso_module
from kaist_cli.v2.klms import secrets as secrets_module
from kaist_cli.cli.output import emit_text
from kaist_cli.v2.contracts import CommandError, CommandResult
from kaist_cli.v2.klms.cache import load_cache_entry, load_cache_value, save_cache_value
from kaist_cli.v2.klms import dashboard as dashboard_module
from kaist_cli.v2.klms.auth_session import clear_auth_session, load_auth_session, save_auth_session
from kaist_cli.v2.klms.auth import AuthService
from kaist_cli.v2.klms.auth import _EasyLoginSignals, _extract_easy_login_error_message, _extract_easy_login_number, _extract_sso_login_view_url, _request_email_otp_delivery, _should_update_easy_login_number, _submit_password_login, looks_login_url
from kaist_cli.v2.klms.assignments import AssignmentService, _extract_assignment_detail_from_html, _extract_assignment_rows_from_calendar_data, _filter_assignments
from kaist_cli.v2.klms.courses import CourseService, _course_is_current_term, _course_matches_query, _course_metadata_map, _discover_courses_from_dashboard, _parse_recent_courses_payload
from kaist_cli.v2.klms.capture import _courseboard_runtime_capture_summary, _extract_courseboard_js_hints
from kaist_cli.v2.klms.config import load_config
from kaist_cli.v2.klms.dashboard import DashboardService, _build_inbox_items, _decorate_today_assignments, _filter_inbox_assignments, _filter_inbox_files, _select_materials, _select_recent_notices
from kaist_cli.v2.klms.discovery import load_recent_courses_args, map_discovery_report
from kaist_cli.v2.klms.files import FileService, _extract_file_items_from_course_contents, _extract_file_items_from_html, _normalize_file_item_metadata, _pull_subdir_for_item, _sanitize_relpath, _synthesize_file_item_from_url, _unwrap_moodle_ajax_payload
from kaist_cli.v2.klms.media_recency import load_media_recency, observe_files, observe_videos
from kaist_cli.v2.klms.models import Assignment, Course, FileItem, Notice, Video
from kaist_cli.v2.klms.notices import NoticeService, _discover_notice_board_ids_from_course_page, _extract_course_ids_from_dashboard, _parse_notice_detail_from_html, _parse_notice_items_from_soup
from kaist_cli.v2.klms.paths import resolve_paths
from kaist_cli.v2.klms.provider_state import ProviderLoad
from kaist_cli.v2.klms.request import RequestService
from kaist_cli.v2.klms.session import KlmsDownloadFallback
from kaist_cli.v2.klms.videos import VideoService, _extract_video_items_from_html, _parse_video_detail_from_html, _parse_video_viewer_from_html


def test_extract_assignment_rows_from_calendar_data_uses_nested_course() -> None:
    assignments = _extract_assignment_rows_from_calendar_data(
        {
            "events": [
                {
                    "id": 44732942,
                    "name": "Quiz 2 10AM is due",
                    "component": "mod_assign",
                    "modulename": "assign",
                    "instance": 812511,
                    "eventtype": "due",
                    "timesort": 1679621100,
                    "formattedtime": '<span class="dimmed_text"><a class="dimmed">Friday, 24 March, 10:25</a></span>',
                    "url": "https://klms.kaist.ac.kr/mod/assign/view.php?id=812511",
                    "course": {
                        "id": 147038,
                        "fullname": "미적분학 I",
                        "fullnamedisplay": "Calculus I",
                        "shortname": "MAS101-(AP)",
                    },
                }
            ]
        },
        base_url="https://klms.kaist.ac.kr",
        auth_mode="profile",
    )
    assert len(assignments) == 1
    assert assignments[0].id == "812511"
    assert assignments[0].title == "Quiz 2 10AM"
    assert assignments[0].course_id == "147038"
    assert assignments[0].course_title == "미적분학 I"
    assert assignments[0].course_title_variants == ("미적분학 I", "Calculus I")
    assert assignments[0].course_code == "MAS101-(AP)"
    assert assignments[0].due_raw == "Friday, 24 March, 10:25"
    assert assignments[0].due_iso == "2023-03-24T01:25:00Z"


def test_filter_assignments_matches_course_query_via_shared_course_aliases() -> None:
    assignments = [
        Assignment(
            id="1210948",
            title="Lab 1 submission",
            url="https://klms.kaist.ac.kr/mod/assign/view.php?id=1210948",
            due_raw="Sunday, 29 March, 23:59",
            due_iso="2026-03-29T14:59:00Z",
            course_id="178434",
            course_title="Operating Systems and Lab(CS.30300_2026_1)",
            course_code="CS.30300_2026_1",
            course_code_base="CS.30300",
        )
    ]
    filtered = _filter_assignments(
        assignments,
        course_id=None,
        course_query="OS",
        since_iso=None,
        limit=None,
        current_term_label="2026 Spring",
        current_term_course_ids={"178434"},
        include_past=False,
    )
    assert [assignment.id for assignment in filtered] == ["1210948"]


@pytest.mark.parametrize(
    "since_iso",
    [
        "2026-03-17T00:00:00.500Z",
        "2026-03-17T09:00:00.500+09:00",
    ],
)
def test_filter_assignments_since_compares_fractional_iso_offsets_as_instants(since_iso: str) -> None:
    assignments = [
        Assignment(
            id="before",
            title="Just before",
            url=None,
            due_raw=None,
            due_iso="2026-03-17T00:00:00.499Z",
            course_id="180871",
            course_title="Introduction to Algorithms",
            course_code="CS.30000_2026_1",
            course_code_base="CS.30000",
        ),
        Assignment(
            id="equal",
            title="Equal instant",
            url=None,
            due_raw=None,
            due_iso="2026-03-17T00:00:00.500Z",
            course_id="180871",
            course_title="Introduction to Algorithms",
            course_code="CS.30000_2026_1",
            course_code_base="CS.30000",
        ),
        Assignment(
            id="after",
            title="Just after",
            url=None,
            due_raw=None,
            due_iso="2026-03-17T00:00:00.501Z",
            course_id="180871",
            course_title="Introduction to Algorithms",
            course_code="CS.30000_2026_1",
            course_code_base="CS.30000",
        ),
    ]

    filtered = _filter_assignments(
        assignments,
        course_id=None,
        course_query=None,
        since_iso=since_iso,
        limit=None,
        include_past=True,
    )

    assert [assignment.id for assignment in filtered] == ["equal", "after"]


def test_filter_assignments_since_preserves_lexical_fallback_for_invalid_timestamps() -> None:
    def assignment(item_id: str, due_iso: str) -> Assignment:
        return Assignment(
            id=item_id,
            title=item_id,
            url=None,
            due_raw=None,
            due_iso=due_iso,
            course_id="180871",
            course_title="Introduction to Algorithms",
            course_code="CS.30000_2026_1",
            course_code_base="CS.30000",
        )

    invalid_floor = _filter_assignments(
        [assignment("lexical-before", "aaa"), assignment("lexical-after", "zzz")],
        course_id=None,
        course_query=None,
        since_iso="not-a-timestamp",
        limit=None,
        include_past=True,
    )
    malformed_due = _filter_assignments(
        [
            assignment("lexical-before", "2026-02-not-a-timestamp"),
            assignment("lexical-after", "2026-04-not-a-timestamp"),
        ],
        course_id=None,
        course_query=None,
        since_iso="2026-03-17T00:00:00Z",
        limit=None,
        include_past=True,
    )

    assert [item.id for item in invalid_floor] == ["lexical-after"]
    assert [item.id for item in malformed_due] == ["lexical-after"]


def test_filter_assignments_defaults_to_current_term_course_ids() -> None:
    assignments = [
        Assignment(
            id="1",
            title="Current HW",
            url=None,
            due_raw=None,
            due_iso="2026-03-17T14:59:00Z",
            course_id="180871",
            course_title="알고리즘 개론",
            course_code="CS.30000_2026_1",
            course_code_base="CS.30000",
            source="api",
            confidence=0.9,
        ),
        Assignment(
            id="2",
            title="Old HW",
            url=None,
            due_raw=None,
            due_iso="2025-11-10T14:59:00Z",
            course_id="170001",
            course_title="Old Course",
            course_code="CS.49200_2025_3",
            course_code_base="CS.49200",
            source="api",
            confidence=0.9,
        ),
    ]

    filtered = _filter_assignments(
        assignments,
        course_id=None,
        course_query=None,
        since_iso=None,
        limit=None,
        current_term_label="2026 Spring",
        current_term_course_ids={"180871"},
        include_past=False,
    )

    assert [assignment.id for assignment in filtered] == ["1"]


def test_filter_assignments_falls_back_to_course_code_term_when_current_course_ids_empty() -> None:
    assignments = [
        Assignment(
            id="1",
            title="Current HW",
            url=None,
            due_raw=None,
            due_iso="2026-03-17T14:59:00Z",
            course_id="180871",
            course_title="알고리즘 개론",
            course_code="CS.30000_2026_1",
            course_code_base="CS.30000",
            source="api",
            confidence=0.9,
        ),
        Assignment(
            id="2",
            title="Old HW",
            url=None,
            due_raw=None,
            due_iso="2025-11-10T14:59:00Z",
            course_id="170001",
            course_title="Old Course",
            course_code="CS492(C)_2025_3",
            course_code_base="CS492(C)",
            source="api",
            confidence=0.9,
        ),
    ]

    filtered = _filter_assignments(
        assignments,
        course_id=None,
        course_query=None,
        since_iso=None,
        limit=None,
        current_term_label="2026 Spring",
        current_term_course_ids=None,
        include_past=False,
    )

    assert [assignment.id for assignment in filtered] == ["1"]


def test_parse_assignment_detail_from_html_extracts_fields() -> None:
    html = """
    <html><body>
      <div id="page-header"><h1>Written Assignment 1</h1></div>
      <nav><a href="/course/view.php?id=180871">알고리즘 개론</a></nav>
      <table>
        <tr><th>Due date</th><td>Tuesday, 17 March 2026, 11:59 PM</td></tr>
      </table>
      <div class="activity-description"><p>Submit the written assignment PDF.</p></div>
    </body></html>
    """
    assignment = _extract_assignment_detail_from_html(
        html,
        base_url="https://klms.kaist.ac.kr",
        url="https://klms.kaist.ac.kr/mod/assign/view.php?id=1210516",
        assignment_id="1210516",
        auth_mode="profile",
    )
    assert assignment.id == "1210516"
    assert assignment.title == "Written Assignment 1"
    assert assignment.course_id == "180871"
    assert assignment.course_title == "알고리즘 개론"
    assert assignment.due_iso == "2026-03-17T14:59:00Z"
    assert assignment.body_text == "Submit the written assignment PDF."
    assert assignment.body_html is not None
    assert assignment.detail_note is None
    assert assignment.detail_available is True


def test_parse_assignment_detail_prefers_non_noise_course_link_and_real_attachments() -> None:
    html = """
    <html><body>
      <div id="page-header"><h1>CS.30000_2026_1: Written Assignment 1</h1></div>
      <div class="menu"><a href="/course/view.php?id=147806">Exam Bank</a></div>
      <nav><a href="/course/view.php?id=180871">알고리즘 개론</a></nav>
      <div id="intro">
        <a href="/mod/resource/index.php?id=180871">Course Contents</a>
        <a href="/pluginfile.php/1845156/mod_assign/introattachment/0/CS30000_Written_Assignment1.pdf?forcedownload=1">CS30000_Written_Assignment1.pdf</a>
      </div>
    </body></html>
    """
    assignment = _extract_assignment_detail_from_html(
        html,
        base_url="https://klms.kaist.ac.kr",
        url="https://klms.kaist.ac.kr/mod/assign/view.php?id=1210516",
        assignment_id="1210516",
        auth_mode="profile",
    )
    assert assignment.title == "Written Assignment 1"
    assert assignment.course_id == "180871"
    assert assignment.course_title == "알고리즘 개론"
    assert [item["filename"] for item in assignment.attachments] == ["CS30000_Written_Assignment1.pdf"]


def test_parse_assignment_detail_extracts_description_from_detail_table() -> None:
    html = """
    <html><body>
      <div id="page-header"><h1>Written Assignment 2</h1></div>
      <nav><a href="/course/view.php?id=180871">알고리즘 개론</a></nav>
      <table>
        <tr><th>설명</th><td><div class="assignment-rich"><p>Implement Dijkstra.</p><p>Submit code and report.</p></div></td></tr>
      </table>
    </body></html>
    """
    assignment = _extract_assignment_detail_from_html(
        html,
        base_url="https://klms.kaist.ac.kr",
        url="https://klms.kaist.ac.kr/mod/assign/view.php?id=1210517",
        assignment_id="1210517",
        auth_mode="profile",
    )
    assert assignment.body_text == "Implement Dijkstra. Submit code and report."
    assert assignment.body_html is not None
    assert "assignment-rich" in assignment.body_html
    assert assignment.detail_note is None
    assert assignment.detail_available is True


def test_parse_assignment_detail_reports_when_body_is_missing() -> None:
    html = """
    <html><body>
      <div id="page-header"><h1>Written Assignment 3</h1></div>
      <nav><a href="/course/view.php?id=180871">알고리즘 개론</a></nav>
      <div id="intro">
        <a href="/pluginfile.php/1845156/mod_assign/introattachment/0/spec.pdf?forcedownload=1">spec.pdf</a>
      </div>
    </body></html>
    """
    assignment = _extract_assignment_detail_from_html(
        html,
        base_url="https://klms.kaist.ac.kr",
        url="https://klms.kaist.ac.kr/mod/assign/view.php?id=1210518",
        assignment_id="1210518",
        auth_mode="profile",
    )
    assert assignment.body_text is None
    assert assignment.detail_available is True
    assert assignment.detail_note == "Assignment attachments were available, but the KLMS page did not expose an extractable description body."


def test_parse_assignment_detail_ignores_submission_action_box() -> None:
    html = """
    <html><body>
      <div id="page-header"><h1>Lab 1 submission</h1></div>
      <nav><a href="/course/view.php?id=178434">Operating Systems and Lab(CS.30300_2026_1)</a></nav>
      <div class="box py-3 generalbox submissionaction">
        <div class="singlebutton"><button>Add submission</button></div>
        <div class="box py-3 boxaligncenter submithelp">You have not made a submission yet.</div>
      </div>
    </body></html>
    """
    assignment = _extract_assignment_detail_from_html(
        html,
        base_url="https://klms.kaist.ac.kr",
        url="https://klms.kaist.ac.kr/mod/assign/view.php?id=1210948",
        assignment_id="1210948",
        auth_mode="profile",
    )
    assert assignment.body_text is None
    assert assignment.detail_available is False
    assert assignment.detail_note == "The KLMS assignment page did not expose an extractable description body."


def test_assignments_show_ignores_mismatched_course_hint(tmp_path: Path, monkeypatch) -> None:
    class FakePage:
        def __init__(self) -> None:
            self.url = "https://klms.kaist.ac.kr/mod/assign/view.php?id=1210516"

        def goto(self, url: str, **kwargs: Any) -> None:  # noqa: ARG002
            self.url = url

        def content(self) -> str:
            return """
            <html><body>
              <div id="page-header"><h1>Written Assignment 1</h1></div>
              <nav><a href="/course/view.php?id=180871">알고리즘 개론</a></nav>
              <div class="activity-description"><p>Submit the written assignment PDF.</p></div>
            </body></html>
            """

        def close(self) -> None:
            return None

    class FakeContext:
        def new_page(self) -> FakePage:
            return FakePage()

    old_home = os.environ.get("KAIST_CLI_HOME")
    os.environ["KAIST_CLI_HOME"] = str(tmp_path / "kaist-home")
    try:
        _write_config(tmp_path)
        paths = resolve_paths()
        auth = AuthService(paths)
        service = AssignmentService(paths, auth)

        def fake_run_authenticated(*, config, headless, accept_downloads, timeout_seconds, callback):  # type: ignore[no-untyped-def]
            return callback(FakeContext(), "profile")

        monkeypatch.setattr(auth, "run_authenticated", fake_run_authenticated)
        result = service.show("1210516", course_id_hint="999999")
    finally:
        if old_home is None:
            os.environ.pop("KAIST_CLI_HOME", None)
        else:
            os.environ["KAIST_CLI_HOME"] = old_home

    assert result.data["id"] == "1210516"
    assert result.data["course_id"] == "180871"
    assert result.data["body_text"] == "Submit the written assignment PDF."

