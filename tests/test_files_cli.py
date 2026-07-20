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


def test_extract_file_items_from_html_skips_video_and_captures_js_helper() -> None:
    html = """
    <html><body>
      <a href="javascript:M.course.format.dayselect(1,180871,0)">1</a>
      <a href="/mod/resource/index.php?id=180871">Course Contents</a>
      <a href="/pluginfile.php/123/file.pdf?forcedownload=1">lecture-notes.pdf</a>
      <a href="/mod/resource/view.php?id=991">Week 1 Slides</a>
      <a href="/mod/folder/view.php?id=992">Starter Code</a>
      <a href="/mod/vod/view.php?id=993">Lecture Video</a>
      <script>
        course.format.downloadFile('https://klms.kaist.ac.kr/mod/page/view.php?id=994', 'Syllabus<span class="dimmed_text"></span>');
      </script>
    </body></html>
    """
    items = _extract_file_items_from_html(
        html,
        base_url="https://klms.kaist.ac.kr",
        course_id="180871",
        course_title="Introduction to Algorithms(CS.30000_2026_1)",
        course_code="CS.30000_2026_1",
        auth_mode="profile",
        source="html:resource-index",
    )
    assert [item.title for item in items] == ["lecture-notes.pdf", "Week 1 Slides", "Starter Code", "Syllabus"]
    assert items[0].downloadable is True
    assert items[0].filename == "file.pdf"
    assert items[2].kind == "folder"
    assert items[2].downloadable is False
    assert items[3].kind == "page"


def test_extract_file_items_from_html_parses_onclick_downloadfile_nodes() -> None:
    html = """
    <html><body>
      <li class="activity resource modtype_resource" id="module-1205280">
        <div class="activityinstance">
          <div class="aalink cursor-pointer" onclick="M.course.format.downloadFile('https://klms.kaist.ac.kr/pluginfile.php/1839743/mod_resource/content/0/intro.pdf', 'intro.pdf')">
            <span class="instancename cursor-pointer">Introduction<span class="accesshide"> File</span></span>
          </div>
        </div>
      </li>
    </body></html>
    """
    items = _extract_file_items_from_html(
        html,
        base_url="https://klms.kaist.ac.kr",
        course_id="180871",
        course_title="Introduction to Algorithms",
        course_code="CS.30000",
        auth_mode="profile",
        source="html:course-view",
    )
    assert len(items) == 1
    assert items[0].id == "1205280"
    assert items[0].title == "Introduction"
    assert items[0].downloadable is True
    assert items[0].filename == "intro.pdf"
    assert items[0].extension == "pdf"
    assert items[0].mime_type == "application/pdf"
    assert items[0].download_url == "https://klms.kaist.ac.kr/pluginfile.php/1839743/mod_resource/content/0/intro.pdf"


def test_extract_file_items_from_html_includes_coursefile_modules() -> None:
    html = """
    <html><body>
      <a href="/mod/coursefile/view.php?id=1212207">Lecture 4</a>
    </body></html>
    """
    items = _extract_file_items_from_html(
        html,
        base_url="https://klms.kaist.ac.kr",
        course_id="178257",
        course_title="근현대 미국사",
        course_code="HSS.20015_2026_1",
        auth_mode="storage_state",
        source="html:course-view",
    )
    assert len(items) == 1
    assert items[0].id == "1212207"
    assert items[0].kind == "file"
    assert items[0].downloadable is True
    assert items[0].url == "https://klms.kaist.ac.kr/mod/coursefile/view.php?id=1212207"
    assert items[0].download_url == "https://klms.kaist.ac.kr/mod/coursefile/view.php?id=1212207"
    assert items[0].extension is None
    assert items[0].mime_type is None


def test_normalize_file_item_metadata_clears_cached_coursefile_script_extension() -> None:
    item = _normalize_file_item_metadata(
        FileItem(
            id="1212207",
            title="Lecture 4",
            url="https://klms.kaist.ac.kr/mod/coursefile/view.php?id=1212207",
            download_url="https://klms.kaist.ac.kr/mod/coursefile/view.php?id=1212207",
            filename=None,
            extension="php",
            mime_type="application/x-httpd-php",
            kind="file",
            downloadable=True,
            course_id="178257",
            course_title="근현대 미국사",
            course_code="HSS.20015_2026_1",
            course_code_base="HSS.20015",
            source="cache",
            confidence=0.7,
            auth_mode="storage_state",
        )
    )
    assert item.filename is None
    assert item.extension is None
    assert item.mime_type is None


def test_extract_file_items_from_course_contents_prefers_api_metadata() -> None:
    items = _extract_file_items_from_course_contents(
        [
            {
                "modules": [
                    {
                        "id": 991,
                        "modname": "resource",
                        "name": "Week 1 Slides",
                        "url": "https://klms.kaist.ac.kr/mod/resource/view.php?id=991",
                        "contents": [
                            {
                                "type": "file",
                                "filename": "week1.pdf",
                                "fileurl": "https://klms.kaist.ac.kr/pluginfile.php/123/week1.pdf?forcedownload=1",
                            }
                        ],
                    },
                    {
                        "id": 992,
                        "modname": "folder",
                        "name": "Starter Code",
                        "url": "https://klms.kaist.ac.kr/mod/folder/view.php?id=992",
                        "contents": [],
                    },
                    {
                        "id": 993,
                        "modname": "url",
                        "name": "Reference Site",
                        "url": "https://klms.kaist.ac.kr/mod/url/view.php?id=993",
                    },
                    {
                        "id": 994,
                        "modname": "resource",
                        "name": "Lecture Video",
                        "url": "https://klms.kaist.ac.kr/mod/resource/view.php?id=994",
                        "contents": [
                            {
                                "type": "file",
                                "filename": "week1.mp4",
                                "fileurl": "https://klms.kaist.ac.kr/pluginfile.php/123/week1.mp4?forcedownload=1",
                            }
                        ],
                    },
                ]
            }
        ],
        base_url="https://klms.kaist.ac.kr",
        course_id="180871",
        course_title="Introduction to Algorithms(CS.30000_2026_1)",
        course_code="CS.30000_2026_1",
        auth_mode="profile",
    )
    assert [item.title for item in items] == ["Week 1 Slides", "Starter Code", "Reference Site"]
    assert items[0].downloadable is True
    assert items[0].download_url == "https://klms.kaist.ac.kr/pluginfile.php/123/week1.pdf?forcedownload=1"
    assert items[0].source == "api:core_course_get_contents"
    assert items[1].kind == "folder"
    assert items[2].kind == "link"


def test_extract_file_items_from_course_contents_includes_coursefile_modules() -> None:
    items = _extract_file_items_from_course_contents(
        [
            {
                "modules": [
                    {
                        "id": 1212207,
                        "modname": "coursefile",
                        "name": "Lecture 4",
                        "url": "https://klms.kaist.ac.kr/mod/coursefile/view.php?id=1212207",
                        "contents": [
                            {
                                "type": "file",
                                "filename": "Lecture4.docx",
                                "fileurl": "https://klms.kaist.ac.kr/pluginfile.php/1846902/mod_coursefile/content/0/Lecture4.docx?forcedownload=1",
                            }
                        ],
                    }
                ]
            }
        ],
        base_url="https://klms.kaist.ac.kr",
        course_id="178257",
        course_title="근현대 미국사",
        course_code="HSS.20015_2026_1",
        auth_mode="storage_state",
    )
    assert len(items) == 1
    assert items[0].id == "1212207"
    assert items[0].kind == "file"
    assert items[0].downloadable is True
    assert items[0].url == "https://klms.kaist.ac.kr/mod/coursefile/view.php?id=1212207"
    assert items[0].download_url == "https://klms.kaist.ac.kr/pluginfile.php/1846902/mod_coursefile/content/0/Lecture4.docx?forcedownload=1"
    assert items[0].filename == "Lecture4.docx"
    assert items[0].extension == "docx"
    assert items[0].mime_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def test_unwrap_moodle_ajax_payload_reports_disabled_service() -> None:
    result = _unwrap_moodle_ajax_payload(
        '[{"error":true,"exception":{"message":"Web service is not available. (It doesn\'t exist or might be disabled.)","errorcode":"servicenotavailable"}}]'
    )
    assert result["status"] == "error"
    assert result["error_code"] == "servicenotavailable"


def test_synthesize_file_item_from_url_preserves_direct_downloads() -> None:
    item = _synthesize_file_item_from_url(
        "https://klms.kaist.ac.kr/pluginfile.php/1845156/mod_assign/introattachment/0/CS30000_Written_Assignment1.pdf?forcedownload=1",
        course_id=None,
        course_title=None,
        course_code=None,
        auth_mode="profile",
    )
    assert item.kind == "file"
    assert item.downloadable is True
    assert item.filename == "CS30000_Written_Assignment1.pdf"
    assert item.download_url == item.url


def test_synthesize_file_item_from_url_marks_coursefile_as_downloadable() -> None:
    item = _synthesize_file_item_from_url(
        "https://klms.kaist.ac.kr/mod/coursefile/view.php?id=1212207",
        course_id=None,
        course_title=None,
        course_code=None,
        auth_mode="storage_state",
    )
    assert item.kind == "file"
    assert item.downloadable is True
    assert item.id == "1212207"
    assert item.download_url == item.url


def test_file_course_map_falls_back_to_termless_dashboard_courses(tmp_path: Path) -> None:
    old_home = os.environ.get("KAIST_CLI_HOME")
    os.environ["KAIST_CLI_HOME"] = str(tmp_path / "kaist-home")
    try:
        _write_config(tmp_path)
        paths = resolve_paths()
        config = load_config(paths)
        service = FileService(paths, AuthService(paths))
        bootstrap = SimpleNamespace(
            dashboard_html="""
            <html><body>
              <select name="year"><option selected>2026</option></select>
              <select name="semester"><option selected>Spring</option></select>
              <a href="/course/view.php?id=180871">Introduction to Algorithms(CS.30000)</a>
              <a href="/course/view.php?id=178223">General Chemistry Experiment I(CH.10002)</a>
            </body></html>
            """
        )
        course_map = service._course_map_for_request(
            bootstrap=bootstrap,
            config=config,
            course_id=None,
            course_query=None,
        )
    finally:
        if old_home is None:
            os.environ.pop("KAIST_CLI_HOME", None)
        else:
            os.environ["KAIST_CLI_HOME"] = old_home

    assert set(course_map.keys()) == {"180871", "178223"}


def test_file_course_map_matches_recent_course_alias_variant(tmp_path: Path, monkeypatch) -> None:
    old_home = os.environ.get("KAIST_CLI_HOME")
    os.environ["KAIST_CLI_HOME"] = str(tmp_path / "kaist-home")
    try:
        _write_config(tmp_path)
        paths = resolve_paths()
        config = load_config(paths)
        service = FileService(paths, AuthService(paths))
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


def test_resolve_target_item_rejects_non_material_url(tmp_path: Path, monkeypatch) -> None:
    old_home = os.environ.get("KAIST_CLI_HOME")
    os.environ["KAIST_CLI_HOME"] = str(tmp_path / "kaist-home")
    try:
        _write_config(tmp_path)
        paths = resolve_paths()
        config = load_config(paths)
        service = FileService(paths, AuthService(paths))
        monkeypatch.setattr(service, "_list_html", lambda **kwargs: [])
        with pytest.raises(CommandError) as exc_info:
            service._resolve_target_item(
                context=object(),
                config=config,
                auth_mode="profile",
                target="https://klms.kaist.ac.kr/course/view.php?id=178223",
            )
    finally:
        if old_home is None:
            os.environ.pop("KAIST_CLI_HOME", None)
        else:
            os.environ["KAIST_CLI_HOME"] = old_home

    assert exc_info.value.code == "CONFIG_INVALID"


def test_download_resolved_item_uses_http_for_direct_file_urls(tmp_path: Path, monkeypatch) -> None:
    old_home = os.environ.get("KAIST_CLI_HOME")
    os.environ["KAIST_CLI_HOME"] = str(tmp_path / "kaist-home")
    try:
        _write_config(tmp_path)
        paths = resolve_paths()
        config = load_config(paths)
        service = FileService(paths, AuthService(paths))
        item = FileItem(
            id="991",
            title="Week 1 Slides",
            url="https://klms.kaist.ac.kr/mod/resource/view.php?id=991",
            download_url="https://klms.kaist.ac.kr/pluginfile.php/123/week1.pdf?forcedownload=1",
            filename="week1.pdf",
            kind="file",
            downloadable=True,
            course_id="180871",
            course_title="Introduction to Algorithms",
            course_code="CS.30000_2026_1",
            course_code_base="CS.30000",
            source="html:file-resolved",
            confidence=0.8,
            auth_mode="profile",
        )
        calls: list[tuple[str, str, float]] = []

        class FakeHttpSession:
            def __init__(self, context, *, base_url: str) -> None:  # type: ignore[no-untyped-def]
                assert base_url == "https://klms.kaist.ac.kr"

            def download_to_path(self, url_or_path: str, *, destination: Path, timeout_seconds: float = 120.0):  # type: ignore[no-untyped-def]
                calls.append((url_or_path, str(destination), timeout_seconds))
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(b"%PDF-http")
                return SimpleNamespace(
                    url=url_or_path,
                    path=str(destination),
                    bytes_written=9,
                )

        class FakeContext:
            def new_page(self):  # type: ignore[no-untyped-def]
                raise AssertionError("browser fallback should not be used for direct pluginfile downloads")

        monkeypatch.setattr("kaist_cli.v2.klms.files.KlmsHttpSession", FakeHttpSession)
        result = service._download_resolved_item(
            context=FakeContext(),
            config=config,
            item=item,
            filename_override=None,
            subdir="pull-http",
            dest=None,
            if_exists="overwrite",
            auth_mode="profile",
        )
    finally:
        if old_home is None:
            os.environ.pop("KAIST_CLI_HOME", None)
        else:
            os.environ["KAIST_CLI_HOME"] = old_home

    assert result["transport"] == "http"
    assert calls
    assert Path(result["path"]).read_bytes() == b"%PDF-http"


def test_download_resolved_item_uses_dest_directory(tmp_path: Path, monkeypatch) -> None:
    old_home = os.environ.get("KAIST_CLI_HOME")
    os.environ["KAIST_CLI_HOME"] = str(tmp_path / "kaist-home")
    try:
        _write_config(tmp_path)
        paths = resolve_paths()
        config = load_config(paths)
        service = FileService(paths, AuthService(paths))
        item = FileItem(
            id="991",
            title="Week 1 Slides",
            url="https://klms.kaist.ac.kr/mod/resource/view.php?id=991",
            download_url="https://klms.kaist.ac.kr/pluginfile.php/123/week1.pdf?forcedownload=1",
            filename="week1.pdf",
            kind="file",
            downloadable=True,
            course_id="180871",
            course_title="Introduction to Algorithms",
            course_code="CS.30000_2026_1",
            course_code_base="CS.30000",
            source="html:file-resolved",
            confidence=0.8,
            auth_mode="profile",
        )
        dest_root = tmp_path / "downloads" / "course"

        class FakeHttpSession:
            def __init__(self, context, *, base_url: str) -> None:  # type: ignore[no-untyped-def]
                assert base_url == "https://klms.kaist.ac.kr"

            def download_to_path(self, url_or_path: str, *, destination: Path, timeout_seconds: float = 120.0):  # type: ignore[no-untyped-def]
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(b"%PDF-http")
                return SimpleNamespace(
                    url=url_or_path,
                    path=str(destination),
                    bytes_written=9,
                )

        monkeypatch.setattr("kaist_cli.v2.klms.files.KlmsHttpSession", FakeHttpSession)
        result = service._download_resolved_item(
            context=SimpleNamespace(),
            config=config,
            item=item,
            filename_override=None,
            subdir=None,
            dest=str(dest_root),
            if_exists="overwrite",
            auth_mode="profile",
        )
    finally:
        if old_home is None:
            os.environ.pop("KAIST_CLI_HOME", None)
        else:
            os.environ["KAIST_CLI_HOME"] = old_home

    assert Path(result["path"]).parent == dest_root
    assert Path(result["path"]).read_bytes() == b"%PDF-http"


def test_download_resolved_item_falls_back_to_browser_when_http_returns_html(tmp_path: Path, monkeypatch) -> None:
    old_home = os.environ.get("KAIST_CLI_HOME")
    os.environ["KAIST_CLI_HOME"] = str(tmp_path / "kaist-home")
    try:
        _write_config(tmp_path)
        paths = resolve_paths()
        config = load_config(paths)
        service = FileService(paths, AuthService(paths))
        item = FileItem(
            id="991",
            title="Week 1 Slides",
            url="https://klms.kaist.ac.kr/mod/resource/view.php?id=991",
            download_url="https://klms.kaist.ac.kr/pluginfile.php/123/week1.pdf?forcedownload=1",
            filename="week1.pdf",
            kind="file",
            downloadable=True,
            course_id="180871",
            course_title="Introduction to Algorithms",
            course_code="CS.30000_2026_1",
            course_code_base="CS.30000",
            source="html:file-resolved",
            confidence=0.8,
            auth_mode="profile",
        )

        class FakeHttpSession:
            def __init__(self, context, *, base_url: str) -> None:  # type: ignore[no-untyped-def]
                pass

            def download_to_path(self, url_or_path: str, *, destination: Path, timeout_seconds: float = 120.0):  # type: ignore[no-untyped-def]
                raise KlmsDownloadFallback("html response")

        class FakeDownload:
            suggested_filename = "week1-from-browser.pdf"

            def save_as(self, path: str) -> None:
                Path(path).write_bytes(b"%PDF-browser")

        class FakeDownloadWaiter:
            def __enter__(self):  # type: ignore[no-untyped-def]
                return self

            def __exit__(self, exc_type, exc, tb):  # type: ignore[no-untyped-def]
                return False

            @property
            def value(self) -> FakeDownload:
                return FakeDownload()

        class FakePage:
            def expect_download(self) -> FakeDownloadWaiter:
                return FakeDownloadWaiter()

            def goto(self, url: str, *, wait_until: str, timeout: int) -> None:
                raise RuntimeError("Download is starting")

            def close(self) -> None:
                return None

        class FakeContext:
            def new_page(self) -> FakePage:
                return FakePage()

        monkeypatch.setattr("kaist_cli.v2.klms.files.KlmsHttpSession", FakeHttpSession)
        result = service._download_resolved_item(
            context=FakeContext(),
            config=config,
            item=item,
            filename_override=None,
            subdir="pull-browser",
            dest=None,
            if_exists="overwrite",
            auth_mode="profile",
        )
    finally:
        if old_home is None:
            os.environ.pop("KAIST_CLI_HOME", None)
        else:
            os.environ["KAIST_CLI_HOME"] = old_home

    assert result["transport"] == "browser"
    assert Path(result["path"]).read_bytes() == b"%PDF-browser"


def test_files_pull_emits_stderr_progress(tmp_path: Path, monkeypatch) -> None:
    old_home = os.environ.get("KAIST_CLI_HOME")
    os.environ["KAIST_CLI_HOME"] = str(tmp_path / "kaist-home")
    try:
        _write_config(tmp_path)
        paths = resolve_paths()
        service = FileService(paths, AuthService(paths))

        monkeypatch.setattr(
            "kaist_cli.v2.klms.files.build_session_bootstrap",
            lambda *args, **kwargs: SimpleNamespace(),
        )
        monkeypatch.setattr(
            service,
            "_list_html",
            lambda **kwargs: [
                FileItem(
                    id="991",
                    title="Week 1 Slides",
                    url="https://klms.kaist.ac.kr/mod/resource/view.php?id=991",
                    download_url="https://klms.kaist.ac.kr/pluginfile.php/123/week1.pdf?forcedownload=1",
                    filename="week1.pdf",
                    kind="file",
                    downloadable=True,
                    course_id="180871",
                    course_title="Introduction to Algorithms",
                    course_code="CS.30000_2026_1",
                    course_code_base="CS.30000",
                    source="html:file-resolved",
                    confidence=0.8,
                    auth_mode="profile",
                )
            ],
        )

        def fake_run_authenticated(*, config, headless, accept_downloads, timeout_seconds, callback):  # type: ignore[no-untyped-def]
            return callback(SimpleNamespace(), "profile")

        monkeypatch.setattr(service._auth, "run_authenticated", fake_run_authenticated)

        def fake_download(*, context, config, item, filename_override=None, subdir=None, dest=None, if_exists="skip", auth_mode):  # type: ignore[no-untyped-def]
            out_dir = Path(dest or paths.files_root) / (subdir or "")
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = out_dir / (item.filename or "file.bin")
            out_path.write_bytes(b"%PDF-http")
            return {
                "ok": True,
                "path": str(out_path),
                "filename": out_path.name,
                "transport": "http",
            }

        monkeypatch.setattr(service, "_download_resolved_item", fake_download)

        stderr = StringIO()
        with redirect_stderr(stderr):
            result = service.pull(course_id="180871", subdir="pull-test")
    finally:
        if old_home is None:
            os.environ.pop("KAIST_CLI_HOME", None)
        else:
            os.environ["KAIST_CLI_HOME"] = old_home

    assert result.data["downloaded_count"] == 1
    assert "[1/1] downloading Week 1 Slides ..." in stderr.getvalue()


def test_files_pull_uses_dest_root(tmp_path: Path, monkeypatch) -> None:
    old_home = os.environ.get("KAIST_CLI_HOME")
    os.environ["KAIST_CLI_HOME"] = str(tmp_path / "kaist-home")
    try:
        _write_config(tmp_path)
        paths = resolve_paths()
        service = FileService(paths, AuthService(paths))
        dest_root = tmp_path / "exports"

        monkeypatch.setattr(
            "kaist_cli.v2.klms.files.build_session_bootstrap",
            lambda *args, **kwargs: SimpleNamespace(),
        )
        monkeypatch.setattr(
            service,
            "_list_html",
            lambda **kwargs: [
                FileItem(
                    id="991",
                    title="Week 1 Slides",
                    url="https://klms.kaist.ac.kr/mod/resource/view.php?id=991",
                    download_url="https://klms.kaist.ac.kr/pluginfile.php/123/week1.pdf?forcedownload=1",
                    filename="week1.pdf",
                    kind="file",
                    downloadable=True,
                    course_id="180871",
                    course_title="Introduction to Algorithms",
                    course_code="CS.30000_2026_1",
                    course_code_base="CS.30000",
                    source="html:file-resolved",
                    confidence=0.8,
                    auth_mode="profile",
                )
            ],
        )

        def fake_run_authenticated(*, config, headless, accept_downloads, timeout_seconds, callback):  # type: ignore[no-untyped-def]
            return callback(SimpleNamespace(), "profile")

        monkeypatch.setattr(service._auth, "run_authenticated", fake_run_authenticated)

        def fake_download(*, context, config, item, filename_override=None, subdir=None, dest=None, if_exists="skip", auth_mode):  # type: ignore[no-untyped-def]
            out_dir = Path(dest or paths.files_root) / (subdir or "")
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = out_dir / (item.filename or "file.bin")
            out_path.write_bytes(b"%PDF-http")
            return {
                "ok": True,
                "path": str(out_path),
                "filename": out_path.name,
                "transport": "http",
            }

        monkeypatch.setattr(service, "_download_resolved_item", fake_download)
        result = service.pull(course_id="180871", dest=str(dest_root))
    finally:
        if old_home is None:
            os.environ.pop("KAIST_CLI_HOME", None)
        else:
            os.environ["KAIST_CLI_HOME"] = old_home

    assert result.data["dest"] == str(dest_root)
    assert result.data["root"] == str(dest_root)
    assert str(dest_root) in result.data["results"][0]["path"]


def test_files_pull_single_course_flattens_course_subdir(tmp_path: Path, monkeypatch) -> None:
    old_home = os.environ.get("KAIST_CLI_HOME")
    os.environ["KAIST_CLI_HOME"] = str(tmp_path / "kaist-home")
    try:
        _write_config(tmp_path)
        paths = resolve_paths()
        service = FileService(paths, AuthService(paths))
        seen_subdirs: list[str | None] = []

        monkeypatch.setattr(
            "kaist_cli.v2.klms.files.build_session_bootstrap",
            lambda *args, **kwargs: SimpleNamespace(),
        )
        monkeypatch.setattr(
            service,
            "_list_html",
            lambda **kwargs: [
                FileItem(
                    id="991",
                    title="Week 1 Slides",
                    url="https://klms.kaist.ac.kr/mod/resource/view.php?id=991",
                    download_url="https://klms.kaist.ac.kr/pluginfile.php/123/week1.pdf?forcedownload=1",
                    filename="week1.pdf",
                    kind="file",
                    downloadable=True,
                    course_id="180871",
                    course_title="Introduction to Algorithms",
                    course_code="CS.30000_2026_1",
                    course_code_base="CS.30000",
                    source="html:file-resolved",
                    confidence=0.8,
                    auth_mode="profile",
                )
            ],
        )

        def fake_run_authenticated(*, config, headless, accept_downloads, timeout_seconds, callback):  # type: ignore[no-untyped-def]
            return callback(SimpleNamespace(), "profile")

        monkeypatch.setattr(service._auth, "run_authenticated", fake_run_authenticated)

        def fake_download(*, context, config, item, filename_override=None, subdir=None, dest=None, if_exists="skip", auth_mode):  # type: ignore[no-untyped-def]
            seen_subdirs.append(subdir)
            out_dir = Path(dest or paths.files_root) / (subdir or "")
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = out_dir / (item.filename or "file.bin")
            out_path.write_bytes(b"%PDF-http")
            return {
                "ok": True,
                "path": str(out_path),
                "filename": out_path.name,
                "transport": "http",
            }

        monkeypatch.setattr(service, "_download_resolved_item", fake_download)
        result = service.pull(course_id="180871", subdir="pull-test")
    finally:
        if old_home is None:
            os.environ.pop("KAIST_CLI_HOME", None)
        else:
            os.environ["KAIST_CLI_HOME"] = old_home

    assert result.data["downloaded_count"] == 1
    assert seen_subdirs == [None]


def test_files_pull_multi_course_keeps_course_subdir(tmp_path: Path, monkeypatch) -> None:
    old_home = os.environ.get("KAIST_CLI_HOME")
    os.environ["KAIST_CLI_HOME"] = str(tmp_path / "kaist-home")
    try:
        _write_config(tmp_path)
        paths = resolve_paths()
        service = FileService(paths, AuthService(paths))
        seen_subdirs: list[str | None] = []

        monkeypatch.setattr(
            "kaist_cli.v2.klms.files.build_session_bootstrap",
            lambda *args, **kwargs: SimpleNamespace(),
        )
        monkeypatch.setattr(
            service,
            "_list_html",
            lambda **kwargs: [
                FileItem(
                    id="991",
                    title="Week 1 Slides",
                    url="https://klms.kaist.ac.kr/mod/resource/view.php?id=991",
                    download_url="https://klms.kaist.ac.kr/pluginfile.php/123/week1.pdf?forcedownload=1",
                    filename="week1.pdf",
                    kind="file",
                    downloadable=True,
                    course_id="180871",
                    course_title="Introduction to Algorithms",
                    course_code="CS.30000_2026_1",
                    course_code_base="CS.30000",
                    source="html:file-resolved",
                    confidence=0.8,
                    auth_mode="profile",
                ),
                FileItem(
                    id="992",
                    title="Lab Manual",
                    url="https://klms.kaist.ac.kr/mod/resource/view.php?id=992",
                    download_url="https://klms.kaist.ac.kr/pluginfile.php/124/manual.pdf?forcedownload=1",
                    filename="manual.pdf",
                    kind="file",
                    downloadable=True,
                    course_id="178223",
                    course_title="General Chemistry Experiment I",
                    course_code="CH.10002_2026_1",
                    course_code_base="CH.10002",
                    source="html:file-resolved",
                    confidence=0.8,
                    auth_mode="profile",
                ),
            ],
        )

        def fake_run_authenticated(*, config, headless, accept_downloads, timeout_seconds, callback):  # type: ignore[no-untyped-def]
            return callback(SimpleNamespace(), "profile")

        monkeypatch.setattr(service._auth, "run_authenticated", fake_run_authenticated)

        def fake_download(*, context, config, item, filename_override=None, subdir=None, dest=None, if_exists="skip", auth_mode):  # type: ignore[no-untyped-def]
            seen_subdirs.append(subdir)
            out_dir = Path(dest or paths.files_root) / (subdir or "")
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = out_dir / (item.filename or "file.bin")
            out_path.write_bytes(b"%PDF-http")
            return {
                "ok": True,
                "path": str(out_path),
                "filename": out_path.name,
                "transport": "http",
            }

        monkeypatch.setattr(service, "_download_resolved_item", fake_download)
        result = service.pull(subdir="pull-test")
    finally:
        if old_home is None:
            os.environ.pop("KAIST_CLI_HOME", None)
        else:
            os.environ["KAIST_CLI_HOME"] = old_home

    assert result.data["downloaded_count"] == 2
    assert seen_subdirs == [
        "CS.30000_2026_1__Introduction_to_Algorithms",
        "CH.10002_2026_1__General_Chemistry_Experiment_I",
    ]


def test_sanitize_relpath_drops_parent_segments() -> None:
    assert str(_sanitize_relpath("../spring26//os/./materials")) == "spring26/os/materials"


def test_pull_subdir_for_item_uses_course_metadata() -> None:
    item = FileItem(
        id="991",
        title="Week 1 Slides",
        url="https://klms.kaist.ac.kr/mod/resource/view.php?id=991",
        download_url="https://klms.kaist.ac.kr/mod/resource/view.php?id=991",
        filename=None,
        kind="file",
        downloadable=True,
        course_id="180871",
        course_title="Introduction to Algorithms",
        course_code="CS.30000_2026_1",
        course_code_base="CS.30000",
        source="html:course-view",
        confidence=0.7,
        auth_mode="profile",
    )
    assert _pull_subdir_for_item(item, base_subdir="spring26", include_course_dir=True) == "spring26/CS.30000_2026_1__Introduction_to_Algorithms"
    assert _pull_subdir_for_item(item, base_subdir="spring26", include_course_dir=False) == "spring26"


def test_file_dashboard_load_uses_recent_stale_cache_without_live_refresh(tmp_path: Path, monkeypatch) -> None:
    old_home = os.environ.get("KAIST_CLI_HOME")
    os.environ["KAIST_CLI_HOME"] = str(tmp_path / "kaist-home")
    try:
        _write_config(tmp_path)
        paths = resolve_paths()
        config = load_config(paths)
        service = FileService(paths, AuthService(paths))
        bootstrap = SimpleNamespace(
            dashboard_html='<a href="/course/view.php?id=180871">Introduction to Algorithms(CS.30000_2026_1)</a>'
        )
        cache_key = service._file_list_cache_key(config, ["180871"])
        save_cache_value(
            paths,
            cache_key,
            [
                {
                    "id": "991",
                    "title": "Week 1 Slides",
                    "url": "https://klms.kaist.ac.kr/mod/resource/view.php?id=991",
                    "download_url": "https://klms.kaist.ac.kr/pluginfile.php/123/week1.pdf?forcedownload=1",
                    "filename": "week1.pdf",
                    "kind": "file",
                    "downloadable": True,
                    "course_id": "180871",
                    "course_title": "Introduction to Algorithms",
                    "course_code": "CS.30000_2026_1",
                    "course_code_base": "CS.30000",
                    "source": "html:course-view",
                    "confidence": 0.7,
                }
            ],
            ttl_seconds=60,
        )
        payload = json.loads(paths.cache_path.read_text(encoding="utf-8"))
        payload["entries"][cache_key]["expires_at"] = 1
        payload["entries"][cache_key]["stored_at"] = time.time()
        paths.cache_path.write_text(json.dumps(payload), encoding="utf-8")

        monkeypatch.setattr(service, "_refresh_file_items", lambda **kwargs: (_ for _ in ()).throw(AssertionError("live refresh should be skipped")))

        result = service.load_for_dashboard(
            context=object(),
            config=config,
            auth_mode="profile",
            bootstrap=bootstrap,
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

