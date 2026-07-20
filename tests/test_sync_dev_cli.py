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


def test_dev_plan_json_envelope(tmp_path: Path) -> None:
    cp = run_cli(tmp_path, "--json", "klms", "dev", "plan")
    assert cp.returncode == 0, cp.stderr
    payload = json.loads(cp.stdout)
    assert payload["ok"] is True
    assert payload["schema"] == "kaist.klms.dev.plan.v1"
    assert payload["data"]["branch"] == "codex/klms-v2"


def test_dev_probe_includes_custom_login_paths_and_provider_candidates(tmp_path: Path) -> None:
    _write_config(tmp_path)

    cp = run_cli(tmp_path, "--json", "klms", "dev", "probe")
    assert cp.returncode == 0, cp.stderr
    payload = json.loads(cp.stdout)
    assert payload["ok"] is True
    assert payload["meta"]["source"] == "probe"
    paths = payload["data"]["login_flow_evidence"]["android_app_paths"]
    assert "/local/applogin/result_login_json.php" in paths
    candidates = payload["data"]["provider_candidates"]
    assert any(candidate["provider"] == "moodle-standard" for candidate in candidates)
    assert any(candidate["provider"] == "klms-ajax" for candidate in candidates)


def test_dev_probe_live_without_config_is_structured(tmp_path: Path) -> None:
    cp = run_cli(tmp_path, "--json", "klms", "dev", "probe", "--live")
    assert cp.returncode == 0, cp.stderr
    payload = json.loads(cp.stdout)
    assert payload["ok"] is True
    assert payload["data"]["validation_mode"] == "live"
    assert payload["data"]["live_validation"]["status"] == "skipped"


def test_cache_round_trip(tmp_path: Path) -> None:
    old_home = os.environ.get("KAIST_CLI_HOME")
    os.environ["KAIST_CLI_HOME"] = str(tmp_path / "kaist-home")
    try:
        paths = resolve_paths()
        save_cache_value(paths, "notice-board-ids::test", ["1", "2"], ttl_seconds=60)
        assert load_cache_value(paths, "notice-board-ids::test") == ["1", "2"]
    finally:
        if old_home is None:
            os.environ.pop("KAIST_CLI_HOME", None)
        else:
            os.environ["KAIST_CLI_HOME"] = old_home


def test_media_recency_store_tracks_first_seen_and_last_seen(tmp_path: Path) -> None:
    old_home = os.environ.get("KAIST_CLI_HOME")
    os.environ["KAIST_CLI_HOME"] = str(tmp_path / "kaist-home")
    try:
        paths = resolve_paths()
        files_once = observe_files(
            paths,
            [
                FileItem(
                    id="1207628",
                    title="01-course overview",
                    url="https://klms.kaist.ac.kr/mod/coursefile/view.php?id=1207628",
                    download_url="https://klms.kaist.ac.kr/mod/coursefile/view.php?id=1207628",
                    filename=None,
                    kind="file",
                    downloadable=True,
                    course_id="178434",
                    course_title="Operating Systems and Lab",
                    course_code="CS.30300_2026_1",
                    course_code_base="CS.30300",
                )
            ],
            observed_at="2026-03-20T00:00:00Z",
        )
        files_twice = observe_files(paths, files_once, observed_at="2026-03-22T00:00:00Z")
        videos_once = observe_videos(
            paths,
            [
                Video(
                    id="1205162",
                    title="Introduction",
                    url="https://klms.kaist.ac.kr/mod/vod/view.php?id=1205162",
                    viewer_url=None,
                    stream_url=None,
                    course_id="180871",
                    course_title="Introduction to Algorithms",
                    course_code="CS.30000_2026_1",
                    course_code_base="CS.30000",
                )
            ],
            observed_at="2026-03-20T00:00:00Z",
        )
        videos_twice = observe_videos(paths, videos_once, observed_at="2026-03-23T00:00:00Z")
        store = load_media_recency(paths)
    finally:
        if old_home is None:
            os.environ.pop("KAIST_CLI_HOME", None)
        else:
            os.environ["KAIST_CLI_HOME"] = old_home

    assert files_twice[0].first_seen_at == "2026-03-20T00:00:00Z"
    assert files_twice[0].last_seen_at == "2026-03-22T00:00:00Z"
    assert videos_twice[0].first_seen_at == "2026-03-20T00:00:00Z"
    assert videos_twice[0].last_seen_at == "2026-03-23T00:00:00Z"
    assert store["files"]
    assert store["videos"]


def test_cache_entry_supports_stale_fallback_metadata(tmp_path: Path) -> None:
    old_home = os.environ.get("KAIST_CLI_HOME")
    os.environ["KAIST_CLI_HOME"] = str(tmp_path / "kaist-home")
    try:
        paths = resolve_paths()
        save_cache_value(paths, "notice-list::test", [{"id": "1"}], ttl_seconds=60)
        payload = json.loads(paths.cache_path.read_text(encoding="utf-8"))
        payload["entries"]["notice-list::test"]["expires_at"] = 0
        paths.cache_path.write_text(json.dumps(payload), encoding="utf-8")
        entry = load_cache_entry(paths, "notice-list::test")
        assert entry is not None
        assert entry["stale"] is True
        assert load_cache_value(paths, "notice-list::test") is None
        assert load_cache_value(paths, "notice-list::test", allow_stale=True) == [{"id": "1"}]
    finally:
        if old_home is None:
            os.environ.pop("KAIST_CLI_HOME", None)
        else:
            os.environ["KAIST_CLI_HOME"] = old_home


def test_request_get_returns_json_body(tmp_path: Path) -> None:
    class FakePage:
        url = "https://klms.kaist.ac.kr/lib/ajax/service.php"

        def evaluate(self, script: str, payload: dict[str, str]) -> dict[str, object]:
            assert payload["url"].endswith("/lib/ajax/service.php")
            return {
                "ok": True,
                "status": 200,
                "url": payload["url"],
                "contentType": "application/json; charset=utf-8",
                "text": '{"ok":true,"items":[1,2,3]}',
            }

        def close(self) -> None:
            return None

    class FakeContext:
        def new_page(self) -> FakePage:
            return FakePage()

    class FakeAuth:
        def run_authenticated(self, *, config, headless, accept_downloads, timeout_seconds, callback):  # type: ignore[no-untyped-def]
            return callback(FakeContext(), "storage_state")

    _write_config(tmp_path)
    old_home = os.environ.get("KAIST_CLI_HOME")
    os.environ["KAIST_CLI_HOME"] = str(tmp_path / "kaist-home")
    try:
        paths = resolve_paths()
        service = RequestService(paths, FakeAuth())  # type: ignore[arg-type]
        result = service.get("/lib/ajax/service.php")
    finally:
        if old_home is None:
            os.environ.pop("KAIST_CLI_HOME", None)
        else:
            os.environ["KAIST_CLI_HOME"] = old_home

    assert result.data["http_status"] == 200
    assert result.data["auth_mode"] == "storage_state"
    assert result.data["body_json"]["ok"] is True
    assert result.data["truncated"] is False


def test_sync_text_output_is_provider_summary() -> None:
    buffer = StringIO()
    with redirect_stdout(buffer):
        emit_text(
            {
                "providers": {
                    "notice_board_ids": {"status": "cache_hit", "item_count": 6, "age_seconds": 12},
                    "notices": {"status": "refreshed", "item_count": 18, "duration_ms": 420, "source": "html"},
                    "files": {"status": "cache_hit", "item_count": 4, "source": "html", "freshness_mode": "cache"},
                },
                "warnings": [],
            },
            command_path="klms sync run",
        )
    output = buffer.getvalue()
    assert "Sync summary:" in output
    assert "notices: refreshed" in output
    assert "18 items" in output
    assert "files: cache_hit" in output


def test_map_discovery_report_classifies_recent_courses_endpoint() -> None:
    report = {
        "endpoints": [
            {
                "method": "POST",
                "url": "https://klms.kaist.ac.kr/lib/ajax/service.php?sesskey=test&info=core_course_get_recent_courses",
                "seen_count": 1,
                "status_codes": [200],
                "content_types": ["application/json; charset=utf-8"],
                "json_like": True,
                "request_headers_subset": {"content-type": "application/json"},
                "has_post_data": True,
                "post_data_size": 97,
                "post_data_preview": '[{"index":0,"methodname":"core_course_get_recent_courses","args":{"userid":"188073","limit":10}}]',
                "response_json_shape": {"type": "array"},
                "response_preview": "[{\"error\":false}]",
            }
        ]
    }
    mapped = map_discovery_report(report=report, source_report_path="/tmp/report.json")
    assert mapped["endpoint_count_unique"] == 1
    assert mapped["recommended_count"] == 1
    endpoint = mapped["recommended_endpoints"][0]
    assert endpoint["category"] == "courses"
    assert endpoint["methodname"] == "core_course_get_recent_courses"


def test_map_discovery_report_classifies_draftfiles_as_files() -> None:
    report = {
        "endpoints": [
            {
                "method": "POST",
                "url": "https://klms.kaist.ac.kr/repository/draftfiles_ajax.php?action=list",
                "seen_count": 1,
                "status_codes": [200],
                "content_types": ["application/json; charset=utf-8"],
                "json_like": True,
                "request_headers_subset": {"content-type": "application/x-www-form-urlencoded; charset=UTF-8"},
                "has_post_data": True,
                "post_data_size": 72,
                "post_data_preview": "sesskey=test&client_id=abc&filepath=%2F&itemid=123",
                "response_json_shape": {"type": "object"},
                "response_preview": "{\"path\":[]}",
            }
        ]
    }
    mapped = map_discovery_report(report=report, source_report_path="/tmp/report.json")
    endpoint = mapped["mapped_endpoints"][0]
    assert endpoint["category"] == "files"
    assert endpoint["recommended_for_cli"] is False


def test_map_discovery_report_classifies_course_contents_as_files() -> None:
    report = {
        "endpoints": [
            {
                "method": "POST",
                "url": "https://klms.kaist.ac.kr/lib/ajax/service.php?sesskey=test&info=core_course_get_contents",
                "seen_count": 1,
                "status_codes": [200],
                "content_types": ["application/json; charset=utf-8"],
                "json_like": True,
                "request_headers_subset": {"content-type": "application/json"},
                "has_post_data": True,
                "post_data_size": 83,
                "post_data_preview": '[{"index":0,"methodname":"core_course_get_contents","args":{"courseid":180871}}]',
                "response_json_shape": {"type": "array"},
                "response_preview": "[{\"error\":false}]",
            }
        ]
    }
    mapped = map_discovery_report(report=report, source_report_path="/tmp/report.json")
    endpoint = mapped["recommended_endpoints"][0]
    assert endpoint["category"] == "files"
    assert endpoint["methodname"] == "core_course_get_contents"


def test_map_discovery_report_downgrades_disabled_course_contents_endpoint() -> None:
    report = {
        "endpoints": [
            {
                "method": "POST",
                "url": "https://klms.kaist.ac.kr/lib/ajax/service.php?sesskey=test&info=core_course_get_contents",
                "seen_count": 1,
                "status_codes": [200],
                "content_types": ["application/json; charset=utf-8"],
                "json_like": True,
                "request_headers_subset": {"content-type": "application/json"},
                "has_post_data": True,
                "post_data_size": 83,
                "post_data_preview": '[{"index":0,"methodname":"core_course_get_contents","args":{"courseid":180871}}]',
                "response_json_shape": {"type": "array"},
                "response_preview": '[{"error":true,"exception":{"message":"Web service is not available.","errorcode":"servicenotavailable"}}]',
            }
        ]
    }
    mapped = map_discovery_report(report=report, source_report_path="/tmp/report.json")
    endpoint = mapped["mapped_endpoints"][0]
    assert endpoint["category"] == "files"
    assert endpoint["recommended_for_cli"] is False


def test_extract_courseboard_js_hints_finds_notice_endpoints() -> None:
    script = '$.ajax({url:www+"/mod/courseboard/ajax.php",data:"type=comment_info&cmid="+aid,type:"post"});$.ajax({url:"action.php",data:"type=category_sortable&idx="+item+"&id="+options.cm_id+"&cid="+options.course_id+"&bid="+options.courseboard_id,type:"post"})'
    hints = _extract_courseboard_js_hints(script, base_url="https://klms.kaist.ac.kr")
    urls = {hint["url"] for hint in hints}
    assert "https://klms.kaist.ac.kr/mod/courseboard/ajax.php" in urls
    assert "https://klms.kaist.ac.kr/mod/courseboard/action.php" in urls


def test_courseboard_runtime_capture_summary_builds_notice_endpoint() -> None:
    summary = _courseboard_runtime_capture_summary(
        [
            {
                "requestId": "cb-1",
                "transport": "jquery_ajax",
                "phase": "config",
                "method": "POST",
                "url": "https://klms.kaist.ac.kr/mod/courseboard/ajax.php",
                "postDataPreview": "type=comment_info&cmid=42",
                "requestHeaders": {"X-Requested-With": "XMLHttpRequest", "Content-Type": "application/x-www-form-urlencoded"},
            },
            {
                "requestId": "cb-1",
                "transport": "jquery_ajax",
                "phase": "response",
                "method": "POST",
                "url": "https://klms.kaist.ac.kr/mod/courseboard/ajax.php",
                "status": 200,
                "contentType": "application/json; charset=utf-8",
                "responsePreview": '{"comments":[{"id":1,"body":"ok"}]}',
            },
        ],
        base_url="https://klms.kaist.ac.kr",
    )
    assert summary["event_count"] == 2
    assert summary["request_event_count"] == 1
    assert summary["response_event_count"] == 1
    assert summary["observed_paths"] == ["/mod/courseboard/ajax.php"]
    endpoint = summary["endpoints"][0]
    assert endpoint["url"] == "https://klms.kaist.ac.kr/mod/courseboard/ajax.php"
    assert endpoint["json_like"] is True
    assert endpoint["hint_only"] is False
    assert endpoint["post_data_preview"] == "type=comment_info&cmid=42"


def test_map_discovery_report_classifies_courseboard_ajax_endpoint() -> None:
    report = {
        "endpoints": [
            {
                "method": "POST",
                "url": "https://klms.kaist.ac.kr/mod/courseboard/ajax.php",
                "seen_count": 1,
                "status_codes": [200],
                "content_types": ["application/json; charset=utf-8"],
                "json_like": True,
                "request_headers_subset": {"content-type": "application/x-www-form-urlencoded; charset=UTF-8"},
                "has_post_data": True,
                "post_data_size": 26,
                "post_data_preview": "type=comment_info&cmid=42",
                "response_json_shape": {"type": "object"},
                "response_preview": '{"comments":[{"id":1}]}',
            }
        ]
    }
    mapped = map_discovery_report(report=report, source_report_path="/tmp/report.json")
    endpoint = mapped["recommended_endpoints"][0]
    assert endpoint["category"] == "notices"
    assert endpoint["recommended_for_cli"] is True


def test_sync_status_reports_cache_entries(tmp_path: Path) -> None:
    old_home = os.environ.get("KAIST_CLI_HOME")
    os.environ["KAIST_CLI_HOME"] = str(tmp_path / "kaist-home")
    try:
        paths = resolve_paths()
        save_cache_value(paths, "notice-board-ids::test", ["1"], ttl_seconds=60)
        save_cache_value(paths, "notice-list::test", [{"id": "n1"}], ttl_seconds=60)
        save_cache_value(paths, "file-list::test", [{"id": "f1"}], ttl_seconds=60)
    finally:
        if old_home is None:
            os.environ.pop("KAIST_CLI_HOME", None)
        else:
            os.environ["KAIST_CLI_HOME"] = old_home

    cp = run_cli(tmp_path, "--json", "klms", "sync", "status")
    assert cp.returncode == 0, cp.stderr
    payload = json.loads(cp.stdout)
    assert payload["ok"] is True
    assert payload["data"]["providers"]["notice_board_ids"]["entry_count"] == 1
    assert payload["data"]["providers"]["notices"]["entry_count"] == 1
    assert payload["data"]["providers"]["files"]["entry_count"] == 1


def test_sync_reset_clears_v2_klms_cache_entries(tmp_path: Path) -> None:
    old_home = os.environ.get("KAIST_CLI_HOME")
    os.environ["KAIST_CLI_HOME"] = str(tmp_path / "kaist-home")
    try:
        paths = resolve_paths()
        save_cache_value(paths, "notice-list::test", [{"id": "n1"}], ttl_seconds=60)
        save_cache_value(paths, "file-list::test", [{"id": "f1"}], ttl_seconds=60)
    finally:
        if old_home is None:
            os.environ.pop("KAIST_CLI_HOME", None)
        else:
            os.environ["KAIST_CLI_HOME"] = old_home

    cp = run_cli(tmp_path, "--json", "klms", "sync", "reset")
    assert cp.returncode == 0, cp.stderr
    payload = json.loads(cp.stdout)
    assert payload["ok"] is True
    assert payload["data"]["removed_entries"] == 2
    assert payload["data"]["providers"]["notices"]["entry_count"] == 0
    assert payload["data"]["providers"]["files"]["entry_count"] == 0

