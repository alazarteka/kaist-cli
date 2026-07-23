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


def test_build_inbox_items_sorts_recent_items_before_undated_files() -> None:
    items = _build_inbox_items(
        assignments=[{"id": "1", "title": "HW1", "due_iso": "2026-03-16T12:00:00+09:00", "url": "/a", "source": "a", "confidence": 0.9}],
        notices=[{"id": "2", "title": "공지", "posted_iso": "2026-03-15T10:00:00+09:00", "url": "/n", "source": "n", "confidence": 0.8}],
        files=[{"id": "3", "title": "notes.pdf", "url": "/f", "downloadable": True, "kind": "file", "source": "f", "confidence": 0.7}],
        limit=10,
    )
    assert [item["kind"] for item in items] == ["assignment", "notice", "file"]


def test_filter_inbox_files_respects_since_iso_and_drops_undated_rows() -> None:
    now = datetime.fromisoformat("2026-03-15T12:00:00+09:00")
    rows = _filter_inbox_files(
        [
            {"id": "1", "downloadable": True, "first_seen_at": "2026-03-15T09:00:00+09:00"},
            {"id": "2", "downloadable": True, "first_seen_at": "2026-03-10T09:00:00+09:00"},
            {"id": "3", "downloadable": True},
            {"id": "4", "downloadable": False, "first_seen_at": "2026-03-15T09:00:00+09:00"},
        ],
        since_iso="2026-03-14T00:00:00+09:00",
        now=now,
    )
    assert [row["id"] for row in rows] == ["1"]


def test_select_materials_prefers_recently_seen_files() -> None:
    rows = _select_materials(
        [
            {"id": "old", "title": "Week 1", "downloadable": True, "course_title": "OS", "first_seen_at": "2026-03-10T09:00:00+09:00"},
            {"id": "fresh", "title": "Week 2", "downloadable": True, "course_title": "OS", "first_seen_at": "2026-03-15T09:00:00+09:00"},
            {"id": "undated", "title": "Week 0", "downloadable": True, "course_title": "OS"},
        ],
        limit=10,
    )
    assert [row["id"] for row in rows] == ["fresh", "old", "undated"]
    assert rows[0]["hours_since_seen"] >= 0


def test_decorate_today_assignments_marks_overdue_and_due_soon() -> None:
    now = datetime.fromisoformat("2026-03-15T12:00:00+09:00")
    rows = _decorate_today_assignments(
        [
            {"id": "0", "title": "ancient", "due_iso": "2026-03-10T09:00:00+09:00"},
            {"id": "1", "title": "old", "due_iso": "2026-03-15T09:00:00+09:00"},
            {"id": "2", "title": "soon", "due_iso": "2026-03-17T09:00:00+09:00"},
        ],
        now=now,
        window_days=7,
        limit=10,
    )
    assert [row["id"] for row in rows] == ["1", "2"]
    assert [row["status"] for row in rows] == ["overdue", "due_soon"]


def test_select_recent_notices_filters_old_posts() -> None:
    now = datetime.fromisoformat("2026-03-15T12:00:00+09:00")
    rows = _select_recent_notices(
        [
            {"id": "1", "title": "fresh", "posted_iso": "2026-03-14T10:00:00+09:00"},
            {"id": "2", "title": "stale", "posted_iso": "2026-03-01T10:00:00+09:00"},
        ],
        now=now,
        notice_days=3,
        limit=10,
    )
    assert [row["id"] for row in rows] == ["1"]


def test_filter_inbox_assignments_drops_far_future_items() -> None:
    now = datetime.fromisoformat("2026-03-15T12:00:00+09:00")
    rows = _filter_inbox_assignments(
        [
            {"id": "1", "due_iso": "2026-03-17T09:00:00+09:00"},
            {"id": "2", "due_iso": "2026-06-01T09:00:00+09:00"},
        ],
        now=now,
    )
    assert [row["id"] for row in rows] == ["1"]


def test_dashboard_today_reuses_one_auth_session_for_all_components(tmp_path: Path) -> None:
    due_iso = (datetime.now(timezone.utc) + timedelta(days=1)).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    class FakeAuth:
        def __init__(self) -> None:
            self.calls = 0

        def run_authenticated_with_state(self, *, config, headless, accept_downloads, timeout_seconds, callback):  # type: ignore[no-untyped-def]
            self.calls += 1
            return callback(object(), "profile", {"final_url": "https://klms.kaist.ac.kr/my/", "html": "<html></html>"})

    class FakeAssignments:
        def __init__(self) -> None:
            self.calls = 0

        def load_for_dashboard(self, *, context, config, auth_mode, course_id=None, since_iso=None, limit=None, bootstrap=None, deadline=None):  # type: ignore[no-untyped-def]
            self.calls += 1
            assert auth_mode == "profile"
            return ProviderLoad(
                items=[{"id": "a1", "title": "HW1", "due_iso": due_iso}],
                source="moodle_ajax",
                capability="full",
                freshness_mode="live",
                cache_hit=False,
                stale=False,
                fetched_at="2026-03-15T00:00:00Z",
                expires_at=None,
                refresh_attempted=True,
            )

    class FakeNotices:
        def __init__(self) -> None:
            self.calls = 0

        def load_for_dashboard(self, *, context, config, auth_mode, notice_board_id=None, max_pages=1, since_iso=None, limit=None, bootstrap=None, deadline=None):  # type: ignore[no-untyped-def]
            self.calls += 1
            return ProviderLoad(
                items=[{"id": "n1", "title": "Notice", "posted_iso": "2026-03-15T10:00:00+09:00"}],
                source="html",
                capability="partial",
                freshness_mode="cache",
                cache_hit=True,
                stale=False,
                fetched_at="2026-03-15T00:00:00Z",
                expires_at="2026-03-15T00:05:00Z",
                refresh_attempted=False,
            )

    class FakeFiles:
        def __init__(self) -> None:
            self.calls = 0

        def load_for_dashboard(self, *, context, config, auth_mode, course_id=None, limit=None, bootstrap=None, deadline=None):  # type: ignore[no-untyped-def]
            self.calls += 1
            return ProviderLoad(
                items=[{"id": "f1", "title": "notes.pdf", "downloadable": True, "kind": "file"}],
                source="html",
                capability="partial",
                freshness_mode="live",
                cache_hit=False,
                stale=False,
                fetched_at="2026-03-15T00:00:01Z",
                expires_at="2026-03-15T00:15:01Z",
                refresh_attempted=True,
            )

    _write_config(tmp_path)
    old_home = os.environ.get("KAIST_CLI_HOME")
    os.environ["KAIST_CLI_HOME"] = str(tmp_path / "kaist-home")
    try:
        paths = resolve_paths()
        auth = FakeAuth()
        assignments = FakeAssignments()
        notices = FakeNotices()
        files = FakeFiles()
        dashboard = DashboardService(paths, auth, assignments, notices, files)
        original_build_bootstrap = dashboard_module.build_session_bootstrap
        dashboard_module.build_session_bootstrap = lambda *args, **kwargs: object()  # type: ignore[assignment]
        try:
            result = dashboard.today(limit=5)
        finally:
            dashboard_module.build_session_bootstrap = original_build_bootstrap  # type: ignore[assignment]
    finally:
        if old_home is None:
            os.environ.pop("KAIST_CLI_HOME", None)
        else:
            os.environ["KAIST_CLI_HOME"] = old_home

    assert auth.calls == 1
    assert assignments.calls == 1
    assert notices.calls == 1
    assert files.calls == 1
    assert result.source == "mixed"
    assert result.data["summary"]["urgent_assignment_count"] == 1
    assert result.data["providers"]["assignments"]["source"] == "moodle_ajax"
    assert result.data["providers"]["notices"]["freshness_mode"] == "cache"
    assert result.data["providers"]["files"]["refresh_attempted"] is True


def test_dashboard_week_summarizes_current_week_items(tmp_path: Path) -> None:
    now = datetime.now().astimezone()
    week_start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    due_iso = (week_start + timedelta(days=2, hours=9)).isoformat(timespec="seconds")
    posted_iso = (week_start + timedelta(days=1, hours=10)).isoformat(timespec="seconds")
    first_seen = (week_start + timedelta(days=3, hours=8)).isoformat(timespec="seconds")

    class FakeAuth:
        def run_authenticated_with_state(self, *, config, headless, accept_downloads, timeout_seconds, callback):  # type: ignore[no-untyped-def]
            return callback(object(), "profile", {"final_url": "https://klms.kaist.ac.kr/my/", "html": "<html></html>"})

    class FakeAssignments:
        def load_for_dashboard(self, **kwargs):  # type: ignore[no-untyped-def]
            return ProviderLoad(
                items=[{"id": "a1", "title": "HW1", "due_iso": due_iso}],
                source="moodle_ajax",
                capability="full",
                freshness_mode="live",
                cache_hit=False,
                stale=False,
                fetched_at="2026-03-15T00:00:00Z",
                expires_at=None,
                refresh_attempted=True,
            )

    class FakeNotices:
        def load_for_dashboard(self, **kwargs):  # type: ignore[no-untyped-def]
            return ProviderLoad(
                items=[{"id": "n1", "title": "Week notice", "posted_iso": posted_iso}],
                source="html",
                capability="partial",
                freshness_mode="live",
                cache_hit=False,
                stale=False,
                fetched_at="2026-03-15T00:00:00Z",
                expires_at=None,
                refresh_attempted=True,
            )

    class FakeFiles:
        def load_for_dashboard(self, **kwargs):  # type: ignore[no-untyped-def]
            return ProviderLoad(
                items=[{"id": "f1", "title": "Week notes", "downloadable": True, "first_seen_at": first_seen}],
                source="html",
                capability="partial",
                freshness_mode="live",
                cache_hit=False,
                stale=False,
                fetched_at="2026-03-15T00:00:01Z",
                expires_at=None,
                refresh_attempted=True,
            )

    _write_config(tmp_path)
    old_home = os.environ.get("KAIST_CLI_HOME")
    os.environ["KAIST_CLI_HOME"] = str(tmp_path / "kaist-home")
    try:
        paths = resolve_paths()
        dashboard = DashboardService(paths, FakeAuth(), FakeAssignments(), FakeNotices(), FakeFiles())
        original_build_bootstrap = dashboard_module.build_session_bootstrap
        dashboard_module.build_session_bootstrap = lambda *args, **kwargs: object()  # type: ignore[assignment]
        try:
            result = dashboard.week(limit=5)
        finally:
            dashboard_module.build_session_bootstrap = original_build_bootstrap  # type: ignore[assignment]
    finally:
        if old_home is None:
            os.environ.pop("KAIST_CLI_HOME", None)
        else:
            os.environ["KAIST_CLI_HOME"] = old_home

    assert result.data["summary"]["assignment_count"] == 1
    assert result.data["summary"]["notice_count"] == 1
    assert result.data["summary"]["material_count"] == 1
    assert result.data["assignments"][0]["id"] == "a1"
    assert result.data["notices"][0]["id"] == "n1"
    assert result.data["materials"][0]["id"] == "f1"


def test_dashboard_inbox_surfaces_stale_cache_warning_from_provider(tmp_path: Path) -> None:
    class FakeAuth:
        def run_authenticated_with_state(self, *, config, headless, accept_downloads, timeout_seconds, callback):  # type: ignore[no-untyped-def]
            return callback(object(), "profile", {"final_url": "https://klms.kaist.ac.kr/my/", "html": "<html></html>"})

    class FakeAssignments:
        def load_for_dashboard(self, **kwargs):  # type: ignore[no-untyped-def]
            return ProviderLoad(
                items=[],
                source="moodle_ajax",
                capability="full",
                freshness_mode="live",
                cache_hit=False,
                stale=False,
                fetched_at="2026-03-15T00:00:00Z",
                expires_at=None,
                refresh_attempted=True,
            )

    class FakeNotices:
        def load_for_dashboard(self, **kwargs):  # type: ignore[no-untyped-def]
            return ProviderLoad(
                items=[{"id": "n1", "title": "Notice", "posted_iso": "2026-03-15T10:00:00+09:00"}],
                source="html",
                capability="partial",
                freshness_mode="cache",
                cache_hit=True,
                stale=True,
                fetched_at="2026-03-14T23:59:00Z",
                expires_at="2026-03-15T00:04:00Z",
                refresh_attempted=True,
                warnings=(
                    {"code": "STALE_CACHE", "message": "Returning stale notice cache because live refresh timed out."},
                    {"code": "LIVE_REFRESH_TIMEOUT", "message": "Notice refresh exceeded the interactive deadline."},
                ),
            )

    class FakeFiles:
        def load_for_dashboard(self, **kwargs):  # type: ignore[no-untyped-def]
            return ProviderLoad(
                items=[],
                source="html",
                capability="partial",
                freshness_mode="live",
                cache_hit=False,
                stale=False,
                fetched_at="2026-03-15T00:00:01Z",
                expires_at="2026-03-15T00:15:01Z",
                refresh_attempted=True,
            )

    _write_config(tmp_path)
    old_home = os.environ.get("KAIST_CLI_HOME")
    os.environ["KAIST_CLI_HOME"] = str(tmp_path / "kaist-home")
    try:
        paths = resolve_paths()
        dashboard = DashboardService(paths, FakeAuth(), FakeAssignments(), FakeNotices(), FakeFiles())
        original_build_bootstrap = dashboard_module.build_session_bootstrap
        dashboard_module.build_session_bootstrap = lambda *args, **kwargs: object()  # type: ignore[assignment]
        try:
            result = dashboard.inbox(limit=5)
        finally:
            dashboard_module.build_session_bootstrap = original_build_bootstrap  # type: ignore[assignment]
    finally:
        if old_home is None:
            os.environ.pop("KAIST_CLI_HOME", None)
        else:
            os.environ["KAIST_CLI_HOME"] = old_home

    assert result.data["providers"]["notices"]["stale"] is True
    warning_codes = [warning["code"] for warning in result.data["warnings"]]
    assert "STALE_CACHE" in warning_codes
    assert "LIVE_REFRESH_TIMEOUT" in warning_codes


def test_dashboard_today_parallelizes_provider_refresh(tmp_path: Path) -> None:
    class FakeAuth:
        def run_authenticated_with_state(self, *, config, headless, accept_downloads, timeout_seconds, callback):  # type: ignore[no-untyped-def]
            return callback(object(), "profile", {"final_url": "https://klms.kaist.ac.kr/my/", "html": "<html></html>"})

    class FakeAssignments:
        def load_for_dashboard(self, **kwargs):  # type: ignore[no-untyped-def]
            return ProviderLoad(
                items=[{"id": "a1", "title": "HW1", "due_iso": "2026-03-17T09:00:00+09:00"}],
                source="moodle_ajax",
                capability="full",
                freshness_mode="live",
                cache_hit=False,
                stale=False,
                fetched_at="2026-03-15T00:00:00Z",
                expires_at=None,
                refresh_attempted=True,
            )

    class SlowNotices:
        def load_for_dashboard(self, **kwargs):  # type: ignore[no-untyped-def]
            time.sleep(0.2)
            return ProviderLoad(
                items=[],
                source="html",
                capability="partial",
                freshness_mode="live",
                cache_hit=False,
                stale=False,
                fetched_at="2026-03-15T00:00:01Z",
                expires_at=None,
                refresh_attempted=True,
            )

    class SlowFiles:
        def load_for_dashboard(self, **kwargs):  # type: ignore[no-untyped-def]
            time.sleep(0.2)
            return ProviderLoad(
                items=[],
                source="html",
                capability="partial",
                freshness_mode="live",
                cache_hit=False,
                stale=False,
                fetched_at="2026-03-15T00:00:01Z",
                expires_at=None,
                refresh_attempted=True,
            )

    _write_config(tmp_path)
    old_home = os.environ.get("KAIST_CLI_HOME")
    os.environ["KAIST_CLI_HOME"] = str(tmp_path / "kaist-home")
    try:
        paths = resolve_paths()
        dashboard = DashboardService(paths, FakeAuth(), FakeAssignments(), SlowNotices(), SlowFiles())
        original_build_bootstrap = dashboard_module.build_session_bootstrap
        dashboard_module.build_session_bootstrap = lambda *args, **kwargs: object()  # type: ignore[assignment]
        try:
            started = time.perf_counter()
            result = dashboard.today(limit=5)
            elapsed = time.perf_counter() - started
        finally:
            dashboard_module.build_session_bootstrap = original_build_bootstrap  # type: ignore[assignment]
    finally:
        if old_home is None:
            os.environ.pop("KAIST_CLI_HOME", None)
        else:
            os.environ["KAIST_CLI_HOME"] = old_home

    assert result.data["providers"]["notices"]["ok"] is True
    assert result.data["providers"]["files"]["ok"] is True
    assert elapsed < 0.35

