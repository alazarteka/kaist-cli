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
from kaist_cli.v2.klms.notices import NoticeService, _discover_notice_board_ids_from_course_page, _extract_course_ids_from_dashboard, _parse_notice_detail_from_html, _parse_notice_items_from_soup, _plan_notice_page_sequence
from kaist_cli.v2.klms.paths import resolve_paths
from kaist_cli.v2.klms.provider_state import ProviderLoad
from kaist_cli.v2.klms.request import RequestService
from kaist_cli.v2.klms.session import KlmsDownloadFallback
from kaist_cli.v2.klms.videos import VideoService, _extract_video_items_from_html, _parse_video_detail_from_html, _parse_video_viewer_from_html


def test_notice_cache_fallback_does_not_reuse_narrower_page_span(tmp_path: Path) -> None:
    old_home = os.environ.get("KAIST_CLI_HOME")
    os.environ["KAIST_CLI_HOME"] = str(tmp_path / "kaist-home")
    try:
        _write_config(tmp_path)
        paths = resolve_paths()
        service = NoticeService(paths, AuthService(paths))
        config = load_config(paths)
        board_ids = ["1178962", "1178963"]
        save_cache_value(
            paths,
            service._notice_list_cache_key(config, board_ids, 1),
            [{"id": "notice-1", "attachments": []}],
            ttl_seconds=600,
        )

        assert service._load_notice_cache_entry(config=config, board_ids=board_ids, max_pages=3) is None

        save_cache_value(
            paths,
            service._notice_list_cache_key(config, board_ids, 3),
            [{"id": "notice-2", "attachments": [{"url": "https://example.com/a.pdf"}]}],
            ttl_seconds=600,
        )

        loaded = service._load_notice_cache_entry(config=config, board_ids=board_ids, max_pages=1)
        assert loaded is not None
        assert loaded["value"][0]["id"] == "notice-1"
    finally:
        if old_home is None:
            os.environ.pop("KAIST_CLI_HOME", None)
        else:
            os.environ["KAIST_CLI_HOME"] = old_home


def test_discover_notice_board_ids_from_course_page_filters_global_boards() -> None:
    html = """
    <html><body>
      <a href="/mod/courseboard/view.php?id=32044">Notice</a>
      <a href="/mod/courseboard/view.php?id=838536">Course Board</a>
    </body></html>
    """
    assert _discover_notice_board_ids_from_course_page(html) == ["838536"]


def test_extract_course_ids_from_dashboard_prefers_non_noise_courses() -> None:
    html = """
    <html><body>
      <a href="/course/view.php?id=147806">Exam Bank</a>
      <a href="/course/view.php?id=180871">Introduction to Algorithms(CS.30000_2026_1)</a>
      <a href="/course/view.php?id=178434">Operating Systems and Lab(CS.34100_2026_1)</a>
    </body></html>
    """
    assert _extract_course_ids_from_dashboard(
        html,
        base_url="https://klms.kaist.ac.kr",
        configured_ids=(),
        exclude_patterns=(),
    ) == ["180871", "178434"]


def test_extract_course_ids_from_dashboard_falls_back_to_termless_current_courses() -> None:
    html = """
    <html><body>
      <select name="year"><option selected>2026</option></select>
      <select name="semester"><option selected>Spring</option></select>
      <a href="/course/view.php?id=178223">General Chemistry Experiment I(CH.10002)</a>
      <a href="/course/view.php?id=180871">Introduction to Algorithms(CS.30000)</a>
      <a href="/course/view.php?id=147806">Exam Bank</a>
    </body></html>
    """
    assert _extract_course_ids_from_dashboard(
        html,
        base_url="https://klms.kaist.ac.kr",
        configured_ids=(),
        exclude_patterns=(),
    ) == ["178223", "180871"]


def test_extract_course_ids_from_dashboard_does_not_append_unrelated_configured_ids_when_filtered() -> None:
    html = """
    <html><body>
      <select name="year"><option selected>2026</option></select>
      <select name="semester"><option selected>Spring</option></select>
      <a href="/course/view.php?id=178223">General Chemistry Experiment I(CH.10002)</a>
      <a href="/course/view.php?id=180871">Introduction to Algorithms(CS.30000)</a>
    </body></html>
    """
    assert _extract_course_ids_from_dashboard(
        html,
        base_url="https://klms.kaist.ac.kr",
        configured_ids=("180871", "178434"),
        exclude_patterns=(),
        course_query="178223",
    ) == ["178223"]


def test_notice_board_resolution_matches_recent_course_alias_variant(tmp_path: Path, monkeypatch) -> None:
    old_home = os.environ.get("KAIST_CLI_HOME")
    os.environ["KAIST_CLI_HOME"] = str(tmp_path / "kaist-home")
    try:
        _write_config(tmp_path)
        paths = resolve_paths()
        config = load_config(paths)
        service = NoticeService(paths, AuthService(paths))
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
            "kaist_cli.v2.klms.notices._load_recent_courses_from_bootstrap",
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
        save_cache_value(
            paths,
            service._notice_board_cache_key(config, ["178434"]),
            {"178434": ["838536"]},
            ttl_seconds=3600,
        )
        board_map = service._resolve_notice_board_map(
            context=SimpleNamespace(),
            config=config,
            explicit_board_id=None,
            course_query="운영체제",
            bootstrap=bootstrap,
        )
    finally:
        if old_home is None:
            os.environ.pop("KAIST_CLI_HOME", None)
        else:
            os.environ["KAIST_CLI_HOME"] = old_home

    assert board_map == {"178434": ["838536"]}


def test_refresh_notice_items_persists_durable_store_and_stops_after_known_page(tmp_path: Path, monkeypatch) -> None:
    old_home = os.environ.get("KAIST_CLI_HOME")
    os.environ["KAIST_CLI_HOME"] = str(tmp_path / "kaist-home")
    try:
        _write_config(tmp_path)
        paths = resolve_paths()
        config = load_config(paths)
        service = NoticeService(paths, AuthService(paths))
        list_html = """
        <html><body>
          <table>
            <tr><th>Title</th><th>Date</th></tr>
            <tr>
              <td><a href="/mod/courseboard/article.php?id=838536&bwid=423326">Exam notice</a></td>
              <td>2026-03-16 18:57</td>
            </tr>
          </table>
          <a href="/mod/courseboard/view.php?id=838536&page=1">2</a>
        </body></html>
        """
        requested_paths: list[str] = []

        class FakeHttp:
            def get_html(self, path: str, *, timeout_seconds: float = 20.0, context: Any | None = None):  # type: ignore[no-untyped-def]  # noqa: ARG002
                requested_paths.append(path)
                if "page=1" in path:
                    raise AssertionError("Incremental refresh should stop before requesting older known pages.")
                return SimpleNamespace(
                    url=f"https://klms.kaist.ac.kr{path}",
                    text=list_html,
                    via="http",
                )

        monkeypatch.setattr(
            "kaist_cli.v2.klms.notices.fetch_html_batch",
            lambda http, paths, **kwargs: {path: http.get_html(path) for path in paths},
        )

        bootstrap = SimpleNamespace(http=FakeHttp())
        enrich_calls: list[list[str | None]] = []

        def fake_enrich(items: list[Notice], **kwargs: Any) -> list[Notice]:  # type: ignore[no-untyped-def]
            enrich_calls.append([item.id for item in items])
            return [
                Notice(
                    board_id="838536",
                    id="423326",
                    title="Exam notice",
                    url="https://klms.kaist.ac.kr/mod/courseboard/article.php?id=838536&bwid=423326",
                    posted_raw="2026-03-16 18:57",
                    posted_iso="2026-03-16T09:57:00Z",
                    author="Prof. Kim",
                    body_text="Important exam note.",
                    attachments=(
                        {
                            "filename": "exam.pdf",
                            "title": "exam.pdf",
                            "url": "https://klms.kaist.ac.kr/pluginfile.php/123/exam.pdf?forcedownload=1",
                        },
                    ),
                    detail_available=True,
                    source="html:courseboard-article",
                    confidence=0.78,
                    auth_mode="profile",
                )
            ]

        monkeypatch.setattr("kaist_cli.v2.klms.notices._enrich_notice_items_from_detail", fake_enrich)

        first = service._refresh_notice_items(
            config=config,
            auth_mode="profile",
            board_ids=["838536"],
            max_pages=1,
            since_iso=None,
            limit=None,
            bootstrap=bootstrap,
            deadline=None,
        )

        payload = json.loads(paths.notice_store_path.read_text(encoding="utf-8"))
        assert "838536:423326" in payload["notices"]
        assert enrich_calls == [["423326"]]
        assert first[0].body_text == "Important exam note."

        monkeypatch.setattr(
            "kaist_cli.v2.klms.notices._enrich_notice_items_from_detail",
            lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("Stored notice detail should be reused.")),
        )

        second = service._refresh_notice_items(
            config=config,
            auth_mode="profile",
            board_ids=["838536"],
            max_pages=3,
            since_iso=None,
            limit=None,
            bootstrap=bootstrap,
            deadline=None,
        )
    finally:
        if old_home is None:
            os.environ.pop("KAIST_CLI_HOME", None)
        else:
            os.environ["KAIST_CLI_HOME"] = old_home

    assert [item.id for item in second] == ["423326"]
    assert second[0].body_text == "Important exam note."
    assert requested_paths == [
        "/mod/courseboard/view.php?id=838536",
        "/mod/courseboard/view.php?id=838536",
    ]


def test_parse_notice_items_from_soup_skips_hidden_rows() -> None:
    soup = BeautifulSoup(
        """
        <table>
          <tr><th>Title</th><th>Date</th></tr>
          <tr><td><a href="/mod/courseboard/article.php?id=1189555&bwid=420856">Lecture videos cannot be watched [1]</a></td><td>2026-03-05</td></tr>
          <tr><td><a href="/mod/courseboard/view.php?id=1189555">This is a hidden post. [2]</a></td><td>2026-03-05</td></tr>
        </table>
        """,
        "html.parser",
    )
    notices = _parse_notice_items_from_soup(
        soup,
        board_id="1189555",
        base_url="https://klms.kaist.ac.kr",
        fallback_url_path="/mod/courseboard/view.php?id=1189555",
    )
    assert [notice.title for notice in notices] == ["Lecture videos cannot be watched [1]"]


def test_parse_notice_detail_from_html_extracts_body_and_attachments() -> None:
    html = """
    <html><body>
      <h1>Midterm 안내</h1>
      <table>
        <tr><th>작성자</th><td>Prof. Kim</td></tr>
        <tr><th>작성일</th><td>2026-03-10 09:00</td></tr>
      </table>
      <div class="article-content"><p>시험 범위를 확인하세요.</p><a href="/pluginfile.php/123/file%20one.pdf">file one.pdf</a></div>
    </body></html>
    """
    notice = _parse_notice_detail_from_html(
        html,
        base_url="https://klms.kaist.ac.kr",
        url="https://klms.kaist.ac.kr/mod/courseboard/article.php?id=838536&bwid=331333",
        auth_mode="profile",
    )
    assert notice.id == "331333"
    assert notice.board_id == "838536"
    assert notice.title == "Midterm 안내"
    assert notice.author == "Prof. Kim"
    assert notice.detail_available is True
    assert notice.attachments[0]["filename"] == "file one.pdf"
    assert notice.attachments[0]["extension"] == "pdf"
    assert notice.attachments[0]["mime_type"] == "application/pdf"


def test_notice_refresh_enriches_returned_items_with_detail_timestamp(tmp_path: Path) -> None:
    class FakeHttp:
        def __init__(self, responses: dict[str, SimpleNamespace]) -> None:
            self._responses = responses

        def get_html(self, url_or_path: str, *, timeout_seconds: float = 20.0, context=None):  # type: ignore[no-untyped-def]
            response = self._responses.get(url_or_path)
            if response is None:
                raise AssertionError(f"unexpected fetch: {url_or_path}")
            return response

    board_path = "/mod/courseboard/view.php?id=838536"
    detail_path = "https://klms.kaist.ac.kr/mod/courseboard/article.php?id=838536&bwid=423326"
    board_html = """
    <html><body>
      <table>
        <tr><th>Title</th><th>Date</th></tr>
        <tr><td><a href="/mod/courseboard/article.php?id=838536&bwid=423326">Exam notice</a></td><td>2026-03-16</td></tr>
      </table>
    </body></html>
    """
    detail_html = """
    <html><body>
      <h1>Exam notice</h1>
      <table>
        <tr><th>작성일</th><td>2026-03-16 09:57</td></tr>
      </table>
      <div class="article-content"><p>Details</p></div>
    </body></html>
    """

    old_home = os.environ.get("KAIST_CLI_HOME")
    os.environ["KAIST_CLI_HOME"] = str(tmp_path / "kaist-home")
    try:
        _write_config(tmp_path)
        paths = resolve_paths()
        service = NoticeService(paths, AuthService(paths))
        bootstrap = SimpleNamespace(
            http=FakeHttp(
                {
                    board_path: SimpleNamespace(url=f"https://klms.kaist.ac.kr{board_path}", text=board_html, via="http"),
                    detail_path: SimpleNamespace(url=detail_path, text=detail_html, via="http"),
                }
            )
        )

        notices = service._refresh_notice_items(
            config=load_config(paths),
            auth_mode="profile",
            board_ids=["838536"],
            max_pages=1,
            since_iso=None,
            limit=1,
            bootstrap=bootstrap,
            deadline=None,
        )
    finally:
        if old_home is None:
            os.environ.pop("KAIST_CLI_HOME", None)
        else:
            os.environ["KAIST_CLI_HOME"] = old_home

    assert len(notices) == 1
    assert notices[0].posted_iso == "2026-03-16T00:57:00Z"


def test_pull_notice_attachments_downloads_attachment_urls(tmp_path: Path, monkeypatch) -> None:
    old_home = os.environ.get("KAIST_CLI_HOME")
    os.environ["KAIST_CLI_HOME"] = str(tmp_path / "kaist-home")
    try:
        _write_config(tmp_path)
        paths = resolve_paths()
        service = NoticeService(paths, AuthService(paths))

        monkeypatch.setattr(
            "kaist_cli.v2.klms.notices.build_session_bootstrap",
            lambda *args, **kwargs: SimpleNamespace(
                dashboard_html='<a href="/course/view.php?id=178223">General Chemistry Lab I(CH.10002_2026_1)</a>'
            ),
        )
        monkeypatch.setattr(
            service,
            "_resolve_notice_board_map",
            lambda **kwargs: {"178223": ["838536"]},
        )
        monkeypatch.setattr(
            service,
            "_list_html",
            lambda **kwargs: [
                Notice(
                    board_id="838536",
                    id="423326",
                    title="Lab Manual",
                    url="https://klms.kaist.ac.kr/mod/courseboard/article.php?id=838536&bwid=423326",
                    posted_raw="2026-03-16 18:57",
                    posted_iso="2026-03-16T09:57:00Z",
                    attachments=(
                        {
                            "title": "week1-manual.pdf",
                            "filename": "week1-manual.pdf",
                            "url": "https://klms.kaist.ac.kr/pluginfile.php/123/week1-manual.pdf?forcedownload=1",
                        },
                    ),
                    source="html:courseboard-article",
                    confidence=0.8,
                    auth_mode="profile",
                )
            ],
        )

        def fake_run_authenticated(*, config, headless, accept_downloads, timeout_seconds, callback):  # type: ignore[no-untyped-def]
            return callback(SimpleNamespace(), "profile")

        monkeypatch.setattr(service._auth, "run_authenticated", fake_run_authenticated)

        def fake_download(self, *, context, config, item, filename_override=None, subdir=None, dest=None, if_exists="skip", auth_mode):  # type: ignore[no-untyped-def]
            out_dir = Path(dest or paths.files_root) / (subdir or "")
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = out_dir / (item.filename or "attachment.bin")
            out_path.write_bytes(b"notice-attachment")
            return {
                "ok": True,
                "path": str(out_path),
                "filename": out_path.name,
                "transport": "http",
            }

        monkeypatch.setattr("kaist_cli.v2.klms.files.FileService.download_item_with_context", fake_download)

        result = service.pull_attachments(course_id="178223", subdir="notice-files")
    finally:
        if old_home is None:
            os.environ.pop("KAIST_CLI_HOME", None)
        else:
            os.environ["KAIST_CLI_HOME"] = old_home

    assert result.data["candidate_count"] == 1
    assert result.data["downloaded_count"] == 1
    assert result.data["results"][0]["transport"] == "http"
    assert result.data["results"][0]["course_id"] == "178223"
    assert result.data["results"][0]["path"].endswith("/notice-files/week1-manual.pdf")


def test_pull_notice_attachments_uses_dest_root(tmp_path: Path, monkeypatch) -> None:
    old_home = os.environ.get("KAIST_CLI_HOME")
    os.environ["KAIST_CLI_HOME"] = str(tmp_path / "kaist-home")
    try:
        _write_config(tmp_path)
        paths = resolve_paths()
        service = NoticeService(paths, AuthService(paths))
        dest_root = tmp_path / "notice-exports"

        monkeypatch.setattr(
            "kaist_cli.v2.klms.notices.build_session_bootstrap",
            lambda *args, **kwargs: SimpleNamespace(
                dashboard_html='<a href="/course/view.php?id=178223">General Chemistry Lab I(CH.10002_2026_1)</a>'
            ),
        )
        monkeypatch.setattr(
            service,
            "_resolve_notice_board_map",
            lambda **kwargs: {"178223": ["838536"]},
        )
        monkeypatch.setattr(
            service,
            "_list_html",
            lambda **kwargs: [
                Notice(
                    board_id="838536",
                    id="423326",
                    title="Lab Manual",
                    url="https://klms.kaist.ac.kr/mod/courseboard/article.php?id=838536&bwid=423326",
                    posted_raw="2026-03-16 18:57",
                    posted_iso="2026-03-16T09:57:00Z",
                    attachments=(
                        {
                            "title": "week1-manual.pdf",
                            "filename": "week1-manual.pdf",
                            "url": "https://klms.kaist.ac.kr/pluginfile.php/123/week1-manual.pdf?forcedownload=1",
                        },
                    ),
                    source="html:courseboard-article",
                    confidence=0.8,
                    auth_mode="profile",
                )
            ],
        )

        def fake_run_authenticated(*, config, headless, accept_downloads, timeout_seconds, callback):  # type: ignore[no-untyped-def]
            return callback(SimpleNamespace(), "profile")

        monkeypatch.setattr(service._auth, "run_authenticated", fake_run_authenticated)

        def fake_download(self, *, context, config, item, filename_override=None, subdir=None, dest=None, if_exists="skip", auth_mode):  # type: ignore[no-untyped-def]
            out_dir = Path(dest or paths.files_root) / (subdir or "")
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = out_dir / (item.filename or "attachment.bin")
            out_path.write_bytes(b"notice-attachment")
            return {
                "ok": True,
                "path": str(out_path),
                "filename": out_path.name,
                "transport": "http",
            }

        monkeypatch.setattr("kaist_cli.v2.klms.files.FileService.download_item_with_context", fake_download)
        result = service.pull_attachments(course_id="178223", dest=str(dest_root))
    finally:
        if old_home is None:
            os.environ.pop("KAIST_CLI_HOME", None)
        else:
            os.environ["KAIST_CLI_HOME"] = old_home

    assert result.data["dest"] == str(dest_root)
    assert result.data["root"] == str(dest_root)
    assert str(dest_root) in result.data["results"][0]["path"]


def test_pull_notice_attachments_single_course_flattens_course_subdir(tmp_path: Path, monkeypatch) -> None:
    old_home = os.environ.get("KAIST_CLI_HOME")
    os.environ["KAIST_CLI_HOME"] = str(tmp_path / "kaist-home")
    try:
        _write_config(tmp_path)
        paths = resolve_paths()
        service = NoticeService(paths, AuthService(paths))
        seen_subdirs: list[str | None] = []

        monkeypatch.setattr(
            "kaist_cli.v2.klms.notices.build_session_bootstrap",
            lambda *args, **kwargs: SimpleNamespace(
                dashboard_html='<a href="/course/view.php?id=178223">General Chemistry Lab I(CH.10002_2026_1)</a>'
            ),
        )
        monkeypatch.setattr(
            service,
            "_resolve_notice_board_map",
            lambda **kwargs: {"178223": ["838536"]},
        )
        monkeypatch.setattr(
            service,
            "_list_html",
            lambda **kwargs: [
                Notice(
                    board_id="838536",
                    id="423326",
                    title="Lab Manual",
                    url="https://klms.kaist.ac.kr/mod/courseboard/article.php?id=838536&bwid=423326",
                    posted_raw="2026-03-16 18:57",
                    posted_iso="2026-03-16T09:57:00Z",
                    attachments=(
                        {
                            "title": "week1-manual.pdf",
                            "filename": "week1-manual.pdf",
                            "url": "https://klms.kaist.ac.kr/pluginfile.php/123/week1-manual.pdf?forcedownload=1",
                        },
                    ),
                    source="html:courseboard-article",
                    confidence=0.8,
                    auth_mode="profile",
                )
            ],
        )

        def fake_run_authenticated(*, config, headless, accept_downloads, timeout_seconds, callback):  # type: ignore[no-untyped-def]
            return callback(SimpleNamespace(), "profile")

        monkeypatch.setattr(service._auth, "run_authenticated", fake_run_authenticated)

        def fake_download(self, *, context, config, item, filename_override=None, subdir=None, dest=None, if_exists="skip", auth_mode):  # type: ignore[no-untyped-def]
            seen_subdirs.append(subdir)
            out_dir = Path(dest or paths.files_root) / (subdir or "")
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = out_dir / (item.filename or "attachment.bin")
            out_path.write_bytes(b"notice-attachment")
            return {
                "ok": True,
                "path": str(out_path),
                "filename": out_path.name,
                "transport": "http",
            }

        monkeypatch.setattr("kaist_cli.v2.klms.files.FileService.download_item_with_context", fake_download)
        result = service.pull_attachments(course_id="178223", subdir="notice-files")
    finally:
        if old_home is None:
            os.environ.pop("KAIST_CLI_HOME", None)
        else:
            os.environ["KAIST_CLI_HOME"] = old_home

    assert result.data["downloaded_count"] == 1
    assert seen_subdirs == [None]


def test_pull_notice_attachments_emits_stderr_progress(tmp_path: Path, monkeypatch) -> None:
    old_home = os.environ.get("KAIST_CLI_HOME")
    os.environ["KAIST_CLI_HOME"] = str(tmp_path / "kaist-home")
    try:
        _write_config(tmp_path)
        paths = resolve_paths()
        service = NoticeService(paths, AuthService(paths))

        monkeypatch.setattr(
            "kaist_cli.v2.klms.notices.build_session_bootstrap",
            lambda *args, **kwargs: SimpleNamespace(
                dashboard_html='<a href="/course/view.php?id=178223">General Chemistry Lab I(CH.10002_2026_1)</a>'
            ),
        )
        monkeypatch.setattr(
            service,
            "_resolve_notice_board_map",
            lambda **kwargs: {"178223": ["838536"]},
        )
        monkeypatch.setattr(
            service,
            "_list_html",
            lambda **kwargs: [
                Notice(
                    board_id="838536",
                    id="423326",
                    title="Lab Manual",
                    url="https://klms.kaist.ac.kr/mod/courseboard/article.php?id=838536&bwid=423326",
                    posted_raw="2026-03-16 18:57",
                    posted_iso="2026-03-16T09:57:00Z",
                    attachments=(
                        {
                            "title": "week1-manual.pdf",
                            "filename": "week1-manual.pdf",
                            "url": "https://klms.kaist.ac.kr/pluginfile.php/123/week1-manual.pdf?forcedownload=1",
                        },
                    ),
                    source="html:courseboard-article",
                    confidence=0.8,
                    auth_mode="profile",
                )
            ],
        )

        def fake_run_authenticated(*, config, headless, accept_downloads, timeout_seconds, callback):  # type: ignore[no-untyped-def]
            return callback(SimpleNamespace(), "profile")

        monkeypatch.setattr(service._auth, "run_authenticated", fake_run_authenticated)

        def fake_download(self, *, context, config, item, filename_override=None, subdir=None, dest=None, if_exists="skip", auth_mode):  # type: ignore[no-untyped-def]
            out_dir = Path(dest or paths.files_root) / (subdir or "")
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = out_dir / (item.filename or "attachment.bin")
            out_path.write_bytes(b"notice-attachment")
            return {
                "ok": True,
                "path": str(out_path),
                "filename": out_path.name,
                "transport": "http",
            }

        monkeypatch.setattr("kaist_cli.v2.klms.files.FileService.download_item_with_context", fake_download)

        stderr = StringIO()
        with redirect_stderr(stderr):
            result = service.pull_attachments(course_id="178223", subdir="notice-files")
    finally:
        if old_home is None:
            os.environ.pop("KAIST_CLI_HOME", None)
        else:
            os.environ["KAIST_CLI_HOME"] = old_home

    assert result.data["downloaded_count"] == 1
    assert "[1/1] downloading week1-manual.pdf ..." in stderr.getvalue()


def test_notice_dashboard_load_uses_recent_stale_cache_without_live_refresh(tmp_path: Path, monkeypatch) -> None:
    old_home = os.environ.get("KAIST_CLI_HOME")
    os.environ["KAIST_CLI_HOME"] = str(tmp_path / "kaist-home")
    try:
        _write_config(tmp_path)
        paths = resolve_paths()
        config = load_config(paths)
        service = NoticeService(paths, AuthService(paths))
        cache_key = service._notice_list_cache_key(config, ["838536"], 1)
        save_cache_value(
            paths,
            cache_key,
            [
                {
                    "board_id": "838536",
                    "id": "423326",
                    "title": "Exam notice",
                    "url": "https://klms.kaist.ac.kr/mod/courseboard/article.php?id=838536&bwid=423326",
                    "posted_raw": "2026-03-16 09:57",
                    "posted_iso": "2026-03-16T00:57:00Z",
                    "source": "html:courseboard",
                    "confidence": 0.7,
                }
            ],
            ttl_seconds=60,
        )
        payload = json.loads(paths.cache_path.read_text(encoding="utf-8"))
        payload["entries"][cache_key]["expires_at"] = 1
        payload["entries"][cache_key]["stored_at"] = time.time()
        paths.cache_path.write_text(json.dumps(payload), encoding="utf-8")

        monkeypatch.setattr(service, "_resolve_notice_board_ids", lambda **kwargs: ["838536"])
        monkeypatch.setattr(service, "_refresh_notice_items", lambda **kwargs: (_ for _ in ()).throw(AssertionError("live refresh should be skipped")))

        result = service.load_for_dashboard(
            context=object(),
            config=config,
            auth_mode="profile",
            bootstrap=SimpleNamespace(),
            prefer_cache=True,
        )
    finally:
        if old_home is None:
            os.environ.pop("KAIST_CLI_HOME", None)
        else:
            os.environ["KAIST_CLI_HOME"] = old_home

    assert result.cache_hit is True
    assert result.refresh_attempted is False
    assert result.warnings == ()


def test_notice_board_resolution_falls_back_to_recent_cached_board_map(tmp_path: Path) -> None:
    old_home = os.environ.get("KAIST_CLI_HOME")
    os.environ["KAIST_CLI_HOME"] = str(tmp_path / "kaist-home")
    try:
        _write_config(tmp_path)
        paths = resolve_paths()
        config = load_config(paths)
        service = NoticeService(paths, AuthService(paths))
        save_cache_value(
            paths,
            service._notice_board_cache_key(config, ["180871", "178434"]),
            {"180871": ["838536"], "178434": ["947531"]},
            ttl_seconds=3600,
        )
        resolved = service._resolve_notice_board_ids(
            context=object(),
            config=config,
            explicit_board_id=None,
            bootstrap=SimpleNamespace(dashboard_html="<html></html>", http=None),
            allow_stale_cache=True,
        )
    finally:
        if old_home is None:
            os.environ.pop("KAIST_CLI_HOME", None)
        else:
            os.environ["KAIST_CLI_HOME"] = old_home

    assert resolved == ["838536", "947531"]

def test_notice_board_map_with_missing_course_response_preserves_canonical_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KAIST_CLI_HOME", str(tmp_path / "kaist-home"))
    _write_config(tmp_path)
    paths = resolve_paths()
    config = load_config(paths)
    service = NoticeService(paths, AuthService(paths))
    course_ids = ["180871", "178434"]
    cache_key = service._notice_board_cache_key(config, course_ids)
    canonical_map = {"180871": ["838536"], "178434": ["947531"]}
    save_cache_value(paths, cache_key, canonical_map, ttl_seconds=60)
    cache_payload = json.loads(paths.cache_path.read_text(encoding="utf-8"))
    cache_payload["entries"][cache_key]["expires_at"] = 0
    paths.cache_path.write_text(json.dumps(cache_payload), encoding="utf-8")
    before = load_cache_entry(paths, cache_key)
    assert before is not None

    dashboard_html = """
    <html><body>
      <a href="/course/view.php?id=180871">Introduction to Algorithms(CS.30000_2026_1)</a>
      <a href="/course/view.php?id=178434">Operating Systems and Lab(CS.34100_2026_1)</a>
    </body></html>
    """
    course_page = "<a href=\"/mod/courseboard/view.php?id=999001\">Course Board</a>"
    response = SimpleNamespace(url="https://klms.kaist.ac.kr/course/view.php?id=180871", text=course_page, via="http")

    def partial_batch(_http: object, request_paths: list[str], **kwargs: object) -> dict[str, SimpleNamespace]:  # noqa: ARG001
        assert request_paths == [
            "/course/view.php?id=180871&section=0",
            "/course/view.php?id=178434&section=0",
        ]
        return {request_paths[0]: response}

    monkeypatch.setattr("kaist_cli.v2.klms.notices.fetch_html_batch", partial_batch)
    board_map = service._resolve_notice_board_map(
        context=object(),
        config=config,
        explicit_board_id=None,
        bootstrap=SimpleNamespace(dashboard_html=dashboard_html, http=object()),
        allow_stale_cache=False,
    )

    after = load_cache_entry(paths, cache_key)
    assert board_map == {"180871": ["999001"]}
    assert after is not None
    assert after["value"] == canonical_map
    assert after["stored_at"] == before["stored_at"]

def test_notice_board_map_fallback_ignores_v2_entries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KAIST_CLI_HOME", str(tmp_path / "kaist-home"))
    _write_config(tmp_path)
    paths = resolve_paths()
    config = load_config(paths)
    service = NoticeService(paths, AuthService(paths))
    canonical_key = service._notice_board_cache_key(config, ["180871"])
    legacy_key = canonical_key.replace("notice-board-map-v3", "notice-board-map-v2", 1)
    save_cache_value(paths, legacy_key, {"180871": ["legacy-board"]}, ttl_seconds=60)

    assert service._fallback_notice_board_ids_from_cache(paths, config) == []

    save_cache_value(paths, canonical_key, {"180871": ["canonical-board"]}, ttl_seconds=60)
    assert service._fallback_notice_board_ids_from_cache(paths, config) == ["canonical-board"]

def test_notice_refresh_fetches_page_one_linked_from_default_page_and_caches_complete_scope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KAIST_CLI_HOME", str(tmp_path / "kaist-home"))
    _write_config(tmp_path)
    paths = resolve_paths()
    config = load_config(paths)
    service = NoticeService(paths, AuthService(paths))
    board_id = "838536"
    first_path = f"/mod/courseboard/view.php?id={board_id}"
    next_path = f"{first_path}&page=1"
    first_html = """
    <table>
      <tr><th>Title</th><th>Date</th></tr>
      <tr><td><a href="/mod/courseboard/article.php?id=838536&bwid=1">New notice</a></td><td>2026-03-03</td></tr>
    </table>
    <a href="/mod/courseboard/view.php?id=838536&page=1">Older</a>
    """
    second_html = """
    <table>
      <tr><th>Title</th><th>Date</th></tr>
      <tr><td><a href="/mod/courseboard/article.php?id=838536&bwid=2">Older notice</a></td><td>2026-03-02</td></tr>
    </table>
    """
    requested_paths: list[str] = []

    class FakeHttp:
        def get_html(self, path: str, *, timeout_seconds: float = 20.0, context: Any | None = None):  # type: ignore[no-untyped-def]  # noqa: ARG002
            requested_paths.append(path)
            assert path == next_path
            return SimpleNamespace(url=f"https://klms.kaist.ac.kr{path}", text=second_html, via="http")

    first_response = SimpleNamespace(url=f"https://klms.kaist.ac.kr{first_path}", text=first_html, via="http")
    monkeypatch.setattr(
        "kaist_cli.v2.klms.notices.fetch_html_batch",
        lambda _http, paths, **kwargs: {path: first_response for path in paths},
    )
    monkeypatch.setattr("kaist_cli.v2.klms.notices._enrich_notice_items_from_detail", lambda items, **kwargs: items)

    notices = service._refresh_notice_items(
        config=config,
        auth_mode="profile",
        board_ids=[board_id],
        max_pages=2,
        since_iso=None,
        limit=None,
        bootstrap=SimpleNamespace(http=FakeHttp()),
        deadline=None,
    )

    assert requested_paths == [next_path]
    assert [notice.id for notice in notices] == ["1", "2"]
    cached_rows = load_cache_value(paths, service._notice_list_cache_key(config, [board_id], 2))
    assert isinstance(cached_rows, list)
    assert [row["id"] for row in cached_rows] == ["1", "2"]

def test_notice_page_plan_fills_gaps_between_discovered_page_links() -> None:
    soup = BeautifulSoup(
        """
        <a href="/mod/courseboard/view.php?id=838536&page=0">1</a>
        <a href="/mod/courseboard/view.php?id=838536&page=2">3</a>
        """,
        "html.parser",
    )

    assert _plan_notice_page_sequence(soup, max_pages=3) == (0, [0, 1, 2])

def test_notice_refresh_discovers_pagination_chain_before_caching_scope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KAIST_CLI_HOME", str(tmp_path / "kaist-home"))
    _write_config(tmp_path)
    paths = resolve_paths()
    config = load_config(paths)
    service = NoticeService(paths, AuthService(paths))
    board_id = "838536"
    first_path = f"/mod/courseboard/view.php?id={board_id}"
    page_one_path = f"{first_path}&page=1"
    page_two_path = f"{first_path}&page=2"
    page_html = {
        first_path: """
        <table><tr><th>Title</th><th>Date</th></tr>
          <tr><td><a href="/mod/courseboard/article.php?id=838536&bwid=1">Page zero</a></td><td>2026-03-03</td></tr>
        </table>
        <a href="/mod/courseboard/view.php?id=838536&page=1">Next</a>
        """,
        page_one_path: """
        <table><tr><th>Title</th><th>Date</th></tr>
          <tr><td><a href="/mod/courseboard/article.php?id=838536&bwid=2">Page one</a></td><td>2026-03-02</td></tr>
        </table>
        <a href="/mod/courseboard/view.php?id=838536&page=2">Next</a>
        """,
        page_two_path: """
        <table><tr><th>Title</th><th>Date</th></tr>
          <tr><td><a href="/mod/courseboard/article.php?id=838536&bwid=3">Page two</a></td><td>2026-03-01</td></tr>
        </table>
        """,
    }
    requested_paths: list[str] = []

    class FakeHttp:
        def get_html(self, path: str, *, timeout_seconds: float = 20.0, context: Any | None = None):  # type: ignore[no-untyped-def]  # noqa: ARG002
            requested_paths.append(path)
            return SimpleNamespace(url=f"https://klms.kaist.ac.kr{path}", text=page_html[path], via="http")

    first_response = SimpleNamespace(url=f"https://klms.kaist.ac.kr{first_path}", text=page_html[first_path], via="http")
    monkeypatch.setattr(
        "kaist_cli.v2.klms.notices.fetch_html_batch",
        lambda _http, paths, **kwargs: {path: first_response for path in paths},
    )
    monkeypatch.setattr("kaist_cli.v2.klms.notices._enrich_notice_items_from_detail", lambda items, **kwargs: items)

    notices = service._refresh_notice_items(
        config=config,
        auth_mode="profile",
        board_ids=[board_id],
        max_pages=3,
        since_iso=None,
        limit=None,
        bootstrap=SimpleNamespace(http=FakeHttp()),
        deadline=None,
    )

    assert requested_paths == [page_one_path, page_two_path]
    assert [notice.id for notice in notices] == ["1", "2", "3"]
    cached_rows = load_cache_value(paths, service._notice_list_cache_key(config, [board_id], 3))
    assert isinstance(cached_rows, list)
    assert [row["id"] for row in cached_rows] == ["1", "2", "3"]

def test_notice_refresh_caches_short_board_when_max_pages_exceeds_available_pages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KAIST_CLI_HOME", str(tmp_path / "kaist-home"))
    _write_config(tmp_path)
    paths = resolve_paths()
    config = load_config(paths)
    service = NoticeService(paths, AuthService(paths))
    board_id = "838536"
    first_path = f"/mod/courseboard/view.php?id={board_id}"
    page_one_path = f"{first_path}&page=1"
    first_html = """
    <table><tr><th>Title</th><th>Date</th></tr>
      <tr><td><a href="/mod/courseboard/article.php?id=838536&bwid=1">Only notice</a></td><td>2026-03-03</td></tr>
    </table>
    """
    second_html = "<table><tr><th>Title</th><th>Date</th></tr></table>"
    requested_paths: list[str] = []

    class FakeHttp:
        def get_html(self, path: str, *, timeout_seconds: float = 20.0, context: Any | None = None):  # type: ignore[no-untyped-def]  # noqa: ARG002
            requested_paths.append(path)
            assert path == page_one_path
            return SimpleNamespace(url=f"https://klms.kaist.ac.kr{path}", text=second_html, via="http")

    first_response = SimpleNamespace(url=f"https://klms.kaist.ac.kr{first_path}", text=first_html, via="http")
    monkeypatch.setattr(
        "kaist_cli.v2.klms.notices.fetch_html_batch",
        lambda _http, paths, **kwargs: {path: first_response for path in paths},
    )
    monkeypatch.setattr("kaist_cli.v2.klms.notices._enrich_notice_items_from_detail", lambda items, **kwargs: items)

    notices = service._refresh_notice_items(
        config=config,
        auth_mode="profile",
        board_ids=[board_id],
        max_pages=3,
        since_iso=None,
        limit=None,
        bootstrap=SimpleNamespace(http=FakeHttp()),
        deadline=None,
    )

    assert requested_paths == [page_one_path]
    assert [notice.id for notice in notices] == ["1"]
    cached_rows = load_cache_value(paths, service._notice_list_cache_key(config, [board_id], 3))
    assert isinstance(cached_rows, list)
    assert [row["id"] for row in cached_rows] == ["1"]

def test_limited_notice_refresh_cannot_overwrite_canonical_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KAIST_CLI_HOME", str(tmp_path / "kaist-home"))
    _write_config(tmp_path)
    paths = resolve_paths()
    config = load_config(paths)
    service = NoticeService(paths, AuthService(paths))
    cache_key = service._notice_list_cache_key(config, ["838536"], 1)
    original = [{"board_id": "838536", "id": "cached", "title": "Canonical", "url": None, "posted_raw": None, "posted_iso": "2026-03-01T00:00:00Z"}]
    save_cache_value(paths, cache_key, original, ttl_seconds=60)
    board_html = """
    <table>
      <tr><th>Title</th><th>Date</th></tr>
      <tr><td><a href="/mod/courseboard/article.php?id=838536&bwid=2">Newer</a></td><td>2026-03-03</td></tr>
      <tr><td><a href="/mod/courseboard/article.php?id=838536&bwid=1">Older</a></td><td>2026-03-02</td></tr>
    </table>
    """
    response = SimpleNamespace(url="https://klms.kaist.ac.kr/mod/courseboard/view.php?id=838536", text=board_html, via="http")
    monkeypatch.setattr(
        "kaist_cli.v2.klms.notices.fetch_html_batch",
        lambda _http, paths, **kwargs: {path: response for path in paths},
    )
    monkeypatch.setattr("kaist_cli.v2.klms.notices._enrich_notice_items_from_detail", lambda items, **kwargs: items)

    items = service._refresh_notice_items(
        config=config,
        auth_mode="profile",
        board_ids=["838536"],
        max_pages=1,
        since_iso=None,
        limit=1,
        bootstrap=SimpleNamespace(http=object()),
        deadline=None,
    )

    assert [item.id for item in items] == ["2", "1"]
    assert load_cache_value(paths, cache_key) == original

def test_complete_notice_cache_supports_later_since_and_limit_without_refresh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KAIST_CLI_HOME", str(tmp_path / "kaist-home"))
    _write_config(tmp_path)
    paths = resolve_paths()
    config = load_config(paths)
    service = NoticeService(paths, AuthService(paths))
    cache_key = service._notice_list_cache_key(config, ["838536"], 1)
    save_cache_value(
        paths,
        cache_key,
        [
            {"board_id": "838536", "id": "old", "title": "Old", "url": None, "posted_raw": None, "posted_iso": "2026-03-01T00:00:00Z"},
            {"board_id": "838536", "id": "new", "title": "New", "url": None, "posted_raw": None, "posted_iso": "2026-03-03T00:00:00Z"},
        ],
        ttl_seconds=60,
    )
    monkeypatch.setattr(service, "_resolve_notice_board_ids", lambda **kwargs: ["838536"])
    monkeypatch.setattr(service, "_refresh_notice_items", lambda **kwargs: (_ for _ in ()).throw(AssertionError("complete cache should be filtered locally")))

    items = service._list_html(
        context=object(),
        config=config,
        auth_mode="profile",
        notice_board_id=None,
        course_id=None,
        course_query=None,
        max_pages=1,
        since_iso="2026-03-02T00:00:00Z",
        limit=1,
        bootstrap=SimpleNamespace(),
    )

    assert [item.id for item in items] == ["new"]

@pytest.mark.parametrize(
    ("cached_rows", "expected_count"),
    [
        ([], 0),
        (
            [
                {
                    "board_id": "838536",
                    "id": "423326",
                    "title": "Exam notice",
                    "url": "https://klms.kaist.ac.kr/mod/courseboard/article.php?id=838536&bwid=423326",
                    "posted_raw": "2026-03-16 09:57",
                    "posted_iso": "2026-03-16T00:57:00Z",
                    "source": "html:courseboard",
                    "confidence": 0.7,
                }
            ],
            1,
        ),
    ],
)
def test_notice_dashboard_live_failure_falls_back_to_fresh_list_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cached_rows: list[dict[str, object]], expected_count: int) -> None:
    monkeypatch.setenv("KAIST_CLI_HOME", str(tmp_path / "kaist-home"))
    _write_config(tmp_path)
    paths = resolve_paths()
    config = load_config(paths)
    service = NoticeService(paths, AuthService(paths))
    save_cache_value(paths, service._notice_list_cache_key(config, ["838536"], 1), cached_rows, ttl_seconds=60)
    monkeypatch.setattr(service, "_resolve_notice_board_ids", lambda **kwargs: ["838536"])
    monkeypatch.setattr(service, "_refresh_notice_items", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("network down")))

    result = service.load_for_dashboard(
        context=object(),
        config=config,
        auth_mode="profile",
        bootstrap=SimpleNamespace(),
        prefer_cache=False,
    )

    assert len(result.items) == expected_count
    assert result.cache_hit is True
    assert result.refresh_attempted is True
    assert result.ok is True
    assert [warning["code"] for warning in result.warnings] == ["LIVE_REFRESH_FAILED"]
    assert result.provider_status()["status"] == "fallback"

def test_notice_dashboard_uses_compatible_bounded_snapshot_after_live_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KAIST_CLI_HOME", str(tmp_path / "kaist-home"))
    _write_config(tmp_path)
    paths = resolve_paths()
    config = load_config(paths)
    service = NoticeService(paths, AuthService(paths))
    board_id = "838536"
    first_path = f"/mod/courseboard/view.php?id={board_id}"
    response = SimpleNamespace(
        url=f"https://klms.kaist.ac.kr{first_path}",
        text="""
        <table><tr><th>Title</th><th>Date</th></tr>
          <tr><td><a href="/mod/courseboard/article.php?id=838536&bwid=2">Newer</a></td><td>2026-03-03</td></tr>
          <tr><td><a href="/mod/courseboard/article.php?id=838536&bwid=1">Older</a></td><td>2026-03-02</td></tr>
        </table>
        """,
        via="http",
    )
    monkeypatch.setattr("kaist_cli.v2.klms.notices.fetch_html_batch", lambda _http, request_paths, **kwargs: {path: response for path in request_paths})
    monkeypatch.setattr("kaist_cli.v2.klms.notices._enrich_notice_items_from_detail", lambda items, **kwargs: items)

    service._refresh_notice_items(
        config=config,
        auth_mode="profile",
        board_ids=[board_id],
        max_pages=1,
        since_iso=None,
        limit=1,
        bootstrap=SimpleNamespace(http=object()),
        deadline=None,
    )

    assert load_cache_entry(paths, service._notice_list_cache_key(config, [board_id], 1)) is None
    assert service._load_notice_snapshot_entry(config=config, board_ids=[board_id], max_pages=1, since_iso=None, limit=1) is not None
    monkeypatch.setattr(service, "_resolve_notice_board_ids", lambda **kwargs: [board_id])
    monkeypatch.setattr(service, "_refresh_notice_items", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("network down")))

    result = service.load_for_dashboard(
        context=object(),
        config=config,
        auth_mode="profile",
        bootstrap=SimpleNamespace(),
        limit=1,
        prefer_cache=False,
    )

    assert [item["id"] for item in result.items] == ["2"]
    assert result.bounded_cache is True
    assert result.provider_status()["status"] == "bounded_fallback"
    assert [warning["code"] for warning in result.warnings] == ["BOUNDED_CACHE", "LIVE_REFRESH_FAILED"]
