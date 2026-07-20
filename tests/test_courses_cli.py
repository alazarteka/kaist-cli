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


def test_recent_courses_args_load_from_api_map(tmp_path: Path) -> None:
    private_root = tmp_path / "kaist-home" / "private" / "klms"
    private_root.mkdir(parents=True, exist_ok=True)
    (private_root / "api_map.json").write_text(
        json.dumps(
            {
                "mapped_endpoints": [
                    {
                        "methodname": "core_course_get_recent_courses",
                        "post_data_preview": '[{"index":0,"methodname":"core_course_get_recent_courses","args":{"userid":"188073","limit":10}}]',
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    old_home = os.environ.get("KAIST_CLI_HOME")
    os.environ["KAIST_CLI_HOME"] = str(tmp_path / "kaist-home")
    try:
        paths = resolve_paths()
        assert load_recent_courses_args(paths, limit=25) == {"userid": "188073", "limit": 25}
    finally:
        if old_home is None:
            os.environ.pop("KAIST_CLI_HOME", None)
        else:
            os.environ["KAIST_CLI_HOME"] = old_home


def test_parse_recent_courses_payload_extracts_courses_and_term() -> None:
    payload = json.dumps(
        [
            {
                "error": False,
                "data": [
                    {
                        "id": 183085,
                        "fullname": "전산학특강",
                        "fullnamedisplay": "Special Topics in CS",
                        "shortname": "CS.49900(F)_2026_1",
                        "viewurl": "https://klms.kaist.ac.kr/course/view.php?id=183085",
                    }
                ],
            }
        ],
        ensure_ascii=False,
    )
    courses = _parse_recent_courses_payload(
        payload,
        base_url="https://klms.kaist.ac.kr",
        auth_mode="profile",
        exclude_patterns=(),
        include_all=False,
        limit=None,
        term_label="2026 Spring",
    )
    assert len(courses) == 1
    assert courses[0].id == "183085"
    assert courses[0].course_code_base == "CS.49900(F)"
    assert courses[0].term_label == "2026 Spring"
    assert courses[0].source == "ajax:core_course_get_recent_courses"
    assert _course_matches_query(courses[0], "Special Topics") is True


def test_course_metadata_map_keeps_dashboard_metadata_and_configured_fallback() -> None:
    course = Course(
        id="180871",
        title="Introduction to Algorithms",
        url="https://klms.kaist.ac.kr/course/view.php?id=180871",
        course_code="CS.30000_2026_1",
        course_code_base="CS.30000",
        term_label="2026 Spring",
        title_variants=("알고리즘 개론",),
    )

    course_map = _course_metadata_map([course], configured_ids=("180871", "178434"))

    assert course_map["180871"]["course_title"] == "Introduction to Algorithms"
    assert course_map["180871"]["course_title_variants"] == ("Introduction to Algorithms", "알고리즘 개론")
    assert course_map["178434"] == {
        "course_id": "178434",
        "course_title": None,
        "course_title_variants": (),
        "course_code": None,
        "course_code_base": None,
        "term_label": None,
    }


def test_course_without_term_label_is_not_current_term() -> None:
    course = Course(
        id="147806",
        title="기출문제은행",
        url="https://klms.kaist.ac.kr/course/view.php?id=147806",
        course_code=None,
        course_code_base=None,
        term_label=None,
        source="html:dashboard",
        confidence=0.5,
    )
    assert _course_is_current_term(course, "2026 Spring", include_past=False) is False


def test_discover_courses_from_dashboard_extracts_course_code_from_parent_card() -> None:
    html = """
    <html><body>
      <div class="course-card">
        <div>CS.30000 Introduction to Algorithms 165 Attendees 3.0 Credit Professors Sunghee Choi</div>
        <a href="/course/view.php?id=180871">Introduction to Algorithms</a>
      </div>
    </body></html>
    """
    courses = _discover_courses_from_dashboard(html, base_url="https://klms.kaist.ac.kr")
    assert len(courses) == 1
    assert courses[0].course_code == "CS.30000"
    assert courses[0].term_label is None


def test_course_matches_query_accepts_english_title_variants_and_abbreviation() -> None:
    course = Course(
        id="178434",
        title="Operating Systems and Lab(CS.30300_2026_1)",
        url="https://klms.kaist.ac.kr/course/view.php?id=178434",
        course_code="CS.30300_2026_1",
        course_code_base="CS.30300",
        term_label="2026 Spring",
    )
    assert _course_matches_query(course, "Operating Systems")
    assert _course_matches_query(course, "OS")
    assert _course_matches_query(course, "CS.30300")


def test_course_matches_query_accepts_korean_title_variant() -> None:
    course = Course(
        id="180871",
        title="운영체제 및 실험",
        url="https://klms.kaist.ac.kr/course/view.php?id=180871",
        course_code="CS.30300_2026_1",
        course_code_base="CS.30300",
        term_label="2026 Spring",
        title_variants=("Operating Systems and Lab",),
    )
    assert _course_matches_query(course, "운영체제")
    assert _course_matches_query(course, "Operating Systems")


def test_course_matches_query_accepts_numeric_course_id() -> None:
    course = Course(
        id="178223",
        title="General Chemistry Experiment I",
        url="https://klms.kaist.ac.kr/course/view.php?id=178223",
        course_code="CH.10002",
        course_code_base="CH.10002",
        term_label=None,
    )
    assert _course_matches_query(course, "178223") is True


def test_course_resolve_returns_matching_aliases(tmp_path: Path, monkeypatch) -> None:
    class FakeAuth:
        def run_authenticated(self, *, config, headless, accept_downloads, timeout_seconds, callback):  # type: ignore[no-untyped-def]
            return callback(object(), "profile")

    _write_config(tmp_path)
    old_home = os.environ.get("KAIST_CLI_HOME")
    os.environ["KAIST_CLI_HOME"] = str(tmp_path / "kaist-home")
    try:
        paths = resolve_paths()
        service = CourseService(paths, FakeAuth())  # type: ignore[arg-type]
        monkeypatch.setattr(
            service,
            "_list_courses_ajax",
            lambda **kwargs: [
                Course(
                    id="178434",
                    title="Operating Systems",
                    url="https://klms.kaist.ac.kr/course/view.php?id=178434",
                    course_code="CS.35000_2026_1",
                    course_code_base="CS.35000",
                    term_label="2026 Spring",
                    title_variants=("Operating Systems", "운영체제"),
                    professors=("Prof. Kim",),
                    source="moodle_ajax",
                    confidence=0.9,
                    auth_mode="profile",
                )
            ],
        )
        monkeypatch.setattr(service, "_enrich_course_professors", lambda **kwargs: kwargs["courses"])
        result = service.resolve(query="운영체제", limit=5)
    finally:
        if old_home is None:
            os.environ.pop("KAIST_CLI_HOME", None)
        else:
            os.environ["KAIST_CLI_HOME"] = old_home

    assert result.data["resolution"] == "unique"
    assert result.data["count"] == 1
    assert "운영체제" in result.data["items"][0]["matched_aliases"]

