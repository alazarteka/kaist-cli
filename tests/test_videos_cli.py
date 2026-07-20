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


def test_extract_video_items_from_html_skips_generic_vod_nav() -> None:
    html = """
    <html><body>
      <nav class="breadcrumb"><a href="/mod/vod/index.php?id=180871">VOD</a></nav>
      <a class="aalink" href="/mod/vod/view.php?id=1205162">Introduction VOD</a>
      <a class="aalink" href="/mod/vod/view.php?id=1205163">algorithm, insertion sort, correctness VOD</a>
    </body></html>
    """
    items = _extract_video_items_from_html(
        html,
        base_url="https://klms.kaist.ac.kr",
        course_id="180871",
        course_title="Introduction to Algorithms(CS.30000_2026_1)",
        course_code="CS.30000_2026_1",
        auth_mode="profile",
        source="html:course-view",
    )
    assert [item.id for item in items] == ["1205162", "1205163"]
    assert [item.title for item in items] == ["Introduction", "algorithm, insertion sort, correctness"]


def test_parse_video_detail_and_viewer_html_extract_watch_and_stream() -> None:
    detail = _parse_video_detail_from_html(
        """
        <html>
          <head><title>CS.30000_2026_1 : Introduction</title></head>
          <body><a href="/mod/vod/viewer/index.php?id=1205162">Watch VOD</a></body>
        </html>
        """,
        base_url="https://klms.kaist.ac.kr",
        fallback_id="1205162",
    )
    viewer = _parse_video_viewer_from_html(
        """
        <html>
          <head><title>Introduction</title></head>
          <body><h1>Introduction</h1><script>clip:{sources:[{type:"video/mp4",src:"/vod1/example.mp4"}]}</script></body>
        </html>
        """,
        base_url="https://klms.kaist.ac.kr",
    )
    assert detail["title"] == "Introduction"
    assert detail["viewer_url"] == "https://klms.kaist.ac.kr/mod/vod/viewer/index.php?id=1205162"
    assert viewer["title"] == "Introduction"
    assert viewer["stream_url"] == "https://klms.kaist.ac.kr/vod1/example.mp4"


def test_video_course_map_matches_recent_course_alias_variant(tmp_path: Path, monkeypatch) -> None:
    old_home = os.environ.get("KAIST_CLI_HOME")
    os.environ["KAIST_CLI_HOME"] = str(tmp_path / "kaist-home")
    try:
        _write_config(tmp_path)
        paths = resolve_paths()
        config = load_config(paths)
        service = VideoService(paths, AuthService(paths))
        bootstrap = SimpleNamespace(
            dashboard_html="""
            <html><body>
              <select name="year"><option selected>2026</option></select>
              <select name="semester"><option selected>Spring</option></select>
              <a href="/course/view.php?id=178434">Operating Systems and Lab(CS.30300_2026_1)</a>
            </body></html>
            """
        )
        monkeypatch.setattr(
            "kaist_cli.v2.klms.courses._load_recent_courses_from_bootstrap",
            lambda *args, **kwargs: [
                Course(
                    id="178434",
                    title="Operating Systems and Lab(CS.30300_2026_1)",
                    url="https://klms.kaist.ac.kr/course/view.php?id=178434",
                    course_code="CS.30300_2026_1",
                    course_code_base="CS.30300",
                    term_label="2026 Spring",
                    title_variants=("운영체제 및 실험",),
                )
            ],
        )
        course_map = service._course_map_for_request(
            bootstrap=bootstrap,
            config=config,
            course_id=None,
            course_query="운영체제",
        )
    finally:
        if old_home is None:
            os.environ.pop("KAIST_CLI_HOME", None)
        else:
            os.environ["KAIST_CLI_HOME"] = old_home

    assert set(course_map.keys()) == {"178434"}


def test_video_list_recent_prefers_first_seen_at_over_id(tmp_path: Path, monkeypatch) -> None:
    old_home = os.environ.get("KAIST_CLI_HOME")
    os.environ["KAIST_CLI_HOME"] = str(tmp_path / "kaist-home")
    try:
        _write_config(tmp_path)
        paths = resolve_paths()
        config = load_config(paths)
        service = VideoService(paths, AuthService(paths))
        bootstrap = SimpleNamespace(
            dashboard_html="""
            <html><body>
              <select name="year"><option selected>2026</option></select>
              <select name="semester"><option selected>Spring</option></select>
              <a href="/course/view.php?id=180871">Introduction to Algorithms(CS.30000_2026_1)</a>
            </body></html>
            """,
            http=object(),
        )
        monkeypatch.setattr(
            "kaist_cli.v2.klms.videos.fetch_html_batch",
            lambda *_args, **_kwargs: {
                "/course/view.php?id=180871&section=0": SimpleNamespace(
                    url="https://klms.kaist.ac.kr/course/view.php?id=180871&section=0",
                    text="""
                    <html><body>
                      <a class="aalink" href="/mod/vod/view.php?id=1205162">Older by id VOD</a>
                      <a class="aalink" href="/mod/vod/view.php?id=1205161">Newer by seen time VOD</a>
                    </body></html>
                    """,
                )
            },
        )
        monkeypatch.setattr(
            "kaist_cli.v2.klms.videos.observe_videos",
            lambda _paths, items: [
                replace(item, first_seen_at="2026-03-10T00:00:00Z")
                if str(item.id) == "1205162"
                else replace(item, first_seen_at="2026-03-20T00:00:00Z")
                for item in items
            ],
        )
        recent_items = service._list_html(
            context=object(),
            config=config,
            auth_mode="storage_state",
            course_id="180871",
            course_query=None,
            limit=None,
            recent=True,
            bootstrap=bootstrap,
        )
    finally:
        if old_home is None:
            os.environ.pop("KAIST_CLI_HOME", None)
        else:
            os.environ["KAIST_CLI_HOME"] = old_home

    assert [item.id for item in recent_items] == ["1205161", "1205162"]

