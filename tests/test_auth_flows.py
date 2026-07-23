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
from kaist_cli.v2.klms.config import KlmsConfig, load_config, maybe_load_config, save_config
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


def test_auth_status_json_envelope(tmp_path: Path) -> None:
    cp = run_cli(tmp_path, "--json", "klms", "auth", "status")
    assert cp.returncode == 0, cp.stderr
    payload = json.loads(cp.stdout)
    assert payload["ok"] is True
    assert payload["schema"] == "kaist.klms.auth.status.v1"
    assert payload["meta"]["capability"] == "partial"
    assert payload["data"]["auth_mode"] == "none"
    assert payload["data"]["configured"] is False


def test_auth_status_verify_returns_refreshed_verification_timestamp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KAIST_CLI_HOME", str(tmp_path / "kaist-home"))
    _write_config(tmp_path)
    paths = resolve_paths()
    auth = AuthService(paths)

    def successful_live_check(**kwargs):  # type: ignore[no-untyped-def]
        auth_module.record_auth_verified(paths)
        return {
            "authenticated": True,
            "auth_mode": "storage_state",
            "final_url": "https://klms.kaist.ac.kr/my/",
        }

    monkeypatch.setattr(auth, "run_authenticated_with_state", successful_live_check)

    result = auth.status(verify=True).data

    assert result["live_check"]["authenticated"] is True
    assert result["last_verified_at"] == auth_module.load_auth_verified(paths)
    assert result["last_verified_at"] is not None


def test_auth_status_verify_reports_transport_failure_as_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KAIST_CLI_HOME", str(tmp_path / "kaist-home"))
    _write_config(tmp_path)
    auth = AuthService(resolve_paths())

    def unavailable_live_check(**kwargs):  # type: ignore[no-untyped-def]
        raise CommandError(
            code="AUTH_CHECK_UNAVAILABLE",
            message="DNS lookup failed",
            retryable=True,
        )

    monkeypatch.setattr(auth, "run_authenticated_with_state", unavailable_live_check)

    result = auth.status(verify=True).data
    live_check = result["live_check"]

    assert live_check["authenticated"] is None
    assert live_check["code"] == "AUTH_CHECK_UNAVAILABLE"
    assert live_check["detail"] == "DNS lookup failed"
    assert live_check["retryable"] is True
    assert isinstance(live_check["checked_at"], str)


def test_auth_status_verify_reports_conclusive_expiry_as_unauthenticated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KAIST_CLI_HOME", str(tmp_path / "kaist-home"))
    _write_config(tmp_path)
    auth = AuthService(resolve_paths())

    def expired_live_check(**kwargs):  # type: ignore[no-untyped-def]
        raise CommandError(code="AUTH_EXPIRED", message="Dashboard redirected to login.")

    monkeypatch.setattr(auth, "run_authenticated_with_state", expired_live_check)

    live_check = auth.status(verify=True).data["live_check"]

    assert live_check["authenticated"] is False
    assert live_check["code"] == "AUTH_EXPIRED"
    assert live_check["detail"] == "Dashboard redirected to login."


def test_auth_status_detects_storage_state_and_cookie_stats(tmp_path: Path) -> None:
    _write_config(tmp_path)
    _write_storage_state(tmp_path)

    cp = run_cli(tmp_path, "--json", "klms", "auth", "status")
    assert cp.returncode == 0, cp.stderr
    payload = json.loads(cp.stdout)
    assert payload["data"]["configured"] is True
    assert payload["data"]["auth_mode"] == "storage_state"
    assert payload["data"]["session_expiry"]["source"] == "cookie_expiry"
    assert payload["data"]["session_expiry"]["overdue"] is False
    assert payload["data"]["session_expiry"]["next_expiry_iso"] is not None
    assert payload["data"]["last_verified_at"] is None
    assert payload["data"]["storage_state_cookie_stats"]["cookie_count"] == 1
    assert payload["data"]["storage_state_cookie_stats"]["next_expiry_iso"] is not None
    assert payload["data"]["storage_state_cookie_stats"]["auth_cookie_count"] == 1
    assert payload["data"]["storage_state_cookie_stats"]["auth_next_expiry_iso"] is not None


def test_auth_status_session_expiry_unknown_without_cookie_expiry(tmp_path: Path) -> None:
    # Bare MoodleSession with expires=-1 has no absolute expiry; status source stays unknown.
    _write_config(tmp_path)
    storage_state_path = tmp_path / "kaist-home" / "private" / "klms" / "storage_state.json"
    storage_state_path.parent.mkdir(parents=True, exist_ok=True)
    storage_state_path.write_text(
        json.dumps(
            {
                "cookies": [
                    {
                        "name": "MoodleSession",
                        "value": "session",
                        "domain": "klms.kaist.ac.kr",
                        "path": "/",
                        "expires": -1,
                    }
                ],
                "origins": [],
            }
        ),
        encoding="utf-8",
    )

    cp = run_cli(tmp_path, "--json", "klms", "auth", "status")
    assert cp.returncode == 0, cp.stderr
    payload = json.loads(cp.stdout)
    assert payload["data"]["session_expiry"]["source"] == "unknown"
    assert "refresh_heuristic" not in payload["data"]
    assert payload["data"]["last_verified_at"] is None


def test_auth_status_ignores_non_auth_cookie_expiry(tmp_path: Path) -> None:
    _write_config(tmp_path)
    storage_state_path = tmp_path / "kaist-home" / "private" / "klms" / "storage_state.json"
    storage_state_path.parent.mkdir(parents=True, exist_ok=True)
    storage_state_path.write_text(
        json.dumps(
            {
                "cookies": [
                    {
                        "name": "MoodleSession",
                        "value": "session",
                        "domain": "klms.kaist.ac.kr",
                        "path": "/",
                        "expires": -1,
                    },
                    {
                        "name": "_ga",
                        "value": "analytics",
                        "domain": "klms.kaist.ac.kr",
                        "path": "/",
                        "expires": 1,
                    },
                ],
                "origins": [],
            }
        ),
        encoding="utf-8",
    )

    cp = run_cli(tmp_path, "--json", "klms", "auth", "status")
    assert cp.returncode == 0, cp.stderr
    payload = json.loads(cp.stdout)

    assert payload["data"]["storage_state_cookie_stats"]["next_expiry_iso"] is not None
    assert payload["data"]["storage_state_cookie_stats"]["auth_next_expiry_iso"] is None
    assert payload["data"]["session_expiry"]["source"] == "unknown"


def test_auth_status_includes_saved_auth_username(tmp_path: Path) -> None:
    _write_config(tmp_path, auth_username="student123")

    cp = run_cli(tmp_path, "--json", "klms", "auth", "status")
    assert cp.returncode == 0, cp.stderr
    payload = json.loads(cp.stdout)
    assert payload["data"]["config"]["auth_username"] == "student123"


def test_auth_status_includes_auth_strategy_and_otp_source(tmp_path: Path) -> None:
    _write_config(tmp_path, auth_username="student123", auth_strategy="email_otp", otp_source="manual")

    cp = run_cli(tmp_path, "--json", "klms", "auth", "status")
    assert cp.returncode == 0, cp.stderr
    payload = json.loads(cp.stdout)
    assert payload["data"]["config"]["auth_strategy"] == "email_otp"
    assert payload["data"]["config"]["otp_source"] == "manual"


def test_load_config_defaults_auth_strategy_for_legacy_config(tmp_path: Path) -> None:
    old_home = os.environ.get("KAIST_CLI_HOME")
    os.environ["KAIST_CLI_HOME"] = str(tmp_path / "kaist-home")
    try:
        config_path = tmp_path / "kaist-home" / "private" / "klms" / "config.toml"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            '\n'.join(
                [
                    'base_url = "https://klms.kaist.ac.kr"',
                    'dashboard_path = "/my/"',
                    'auth_username = "student123"',
                    "course_ids = []",
                    "notice_board_ids = []",
                    "exclude_course_title_patterns = []",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        config = load_config(resolve_paths())
    finally:
        if old_home is None:
            os.environ.pop("KAIST_CLI_HOME", None)
        else:
            os.environ["KAIST_CLI_HOME"] = old_home

    assert config.auth_strategy == "easy_login"
    assert config.otp_source is None


def test_auth_refresh_uses_saved_auth_username(tmp_path: Path, monkeypatch) -> None:
    _write_config(tmp_path, auth_username="student123")
    old_home = os.environ.get("KAIST_CLI_HOME")
    os.environ["KAIST_CLI_HOME"] = str(tmp_path / "kaist-home")
    try:
        auth = AuthService(resolve_paths())
        calls: list[dict[str, object]] = []

        def fake_login(*, base_url=None, dashboard_path=None, username=None, wait_seconds=180.0):
            calls.append(
                {
                    "base_url": base_url,
                    "dashboard_path": dashboard_path,
                    "username": username,
                    "wait_seconds": wait_seconds,
                }
            )
            return CommandResult(data={"ok": True}, source="bootstrap", capability="partial")

        monkeypatch.setattr(auth, "login", fake_login)
        auth.refresh()
        assert calls
        assert calls[0]["username"] == "student123"
    finally:
        if old_home is None:
            os.environ.pop("KAIST_CLI_HOME", None)
        else:
            os.environ["KAIST_CLI_HOME"] = old_home


def test_auth_refresh_dispatches_to_email_otp_flow(tmp_path: Path, monkeypatch) -> None:
    _write_config(tmp_path, auth_username="student123", auth_strategy="email_otp", otp_source="manual")
    old_home = os.environ.get("KAIST_CLI_HOME")
    os.environ["KAIST_CLI_HOME"] = str(tmp_path / "kaist-home")
    try:
        auth = AuthService(resolve_paths())
        calls: list[dict[str, object]] = []

        def fake_begin_refresh(*, base_url=None, dashboard_path=None, username=None, wait_seconds=180.0):
            calls.append(
                {
                    "base_url": base_url,
                    "dashboard_path": dashboard_path,
                    "username": username,
                    "wait_seconds": wait_seconds,
                }
            )
            return CommandResult(data={"ok": True, "state": "waiting_for_email_otp"}, source="bootstrap", capability="partial")

        def unexpected_login(**kwargs):  # type: ignore[no-untyped-def]
            raise AssertionError(f"login should not be called: {kwargs}")

        monkeypatch.setattr(auth, "begin_refresh", fake_begin_refresh)
        monkeypatch.setattr(auth, "login", unexpected_login)
        result = auth.refresh(wait_seconds=222.0)
    finally:
        if old_home is None:
            os.environ.pop("KAIST_CLI_HOME", None)
        else:
            os.environ["KAIST_CLI_HOME"] = old_home

    assert result.data["state"] == "waiting_for_email_otp"
    assert calls == [
        {
            "base_url": None,
            "dashboard_path": None,
            "username": "student123",
            "wait_seconds": 222.0,
        }
    ]


def test_auth_setup_email_otp_persists_config_and_secret(tmp_path: Path, monkeypatch) -> None:
    old_home = os.environ.get("KAIST_CLI_HOME")
    os.environ["KAIST_CLI_HOME"] = str(tmp_path / "kaist-home")
    os.environ["KAIST_PASSWORD_FOR_TEST"] = "hunter2"
    try:
        paths = resolve_paths()

        secret_store = _FakeSecretStore()
        auth = AuthService(paths, secret_store=secret_store)
        result = auth.setup_email_otp(
            base_url="https://klms.kaist.ac.kr",
            username="student123",
            otp_source="manual",
            password_env="KAIST_PASSWORD_FOR_TEST",
        )
        config = load_config(paths)
    finally:
        os.environ.pop("KAIST_PASSWORD_FOR_TEST", None)
        if old_home is None:
            os.environ.pop("KAIST_CLI_HOME", None)
        else:
            os.environ["KAIST_CLI_HOME"] = old_home

    assert result.data["auth_strategy"] == "email_otp"
    assert result.data["otp_source"] == "manual"
    assert result.data["secret_configured"] is True
    assert secret_store.saved == ("student123", "hunter2")
    assert config.auth_username == "student123"
    assert config.auth_strategy == "email_otp"
    assert config.otp_source == "manual"


def test_auth_setup_email_otp_is_config_only_by_default(tmp_path: Path) -> None:
    old_home = os.environ.get("KAIST_CLI_HOME")
    os.environ["KAIST_CLI_HOME"] = str(tmp_path / "kaist-home")
    try:
        paths = resolve_paths()

        secret_store = _FakeSecretStore()
        auth = AuthService(paths, secret_store=secret_store)
        result = auth.setup_email_otp(
            base_url="https://klms.kaist.ac.kr",
            username="student123",
            otp_source="manual",
        )
        config = load_config(paths)
    finally:
        if old_home is None:
            os.environ.pop("KAIST_CLI_HOME", None)
        else:
            os.environ["KAIST_CLI_HOME"] = old_home

    assert result.data["auth_strategy"] == "email_otp"
    assert result.data["secret_configured"] is False
    assert "store-email-otp-secret --username student123" in result.data["next_step"]
    assert secret_store.saved is None
    assert config.auth_username == "student123"
    assert config.auth_strategy == "email_otp"


def test_auth_store_email_otp_secret_uses_saved_username(tmp_path: Path) -> None:
    _write_config(tmp_path, auth_username="student123", auth_strategy="email_otp", otp_source="manual")
    old_home = os.environ.get("KAIST_CLI_HOME")
    os.environ["KAIST_CLI_HOME"] = str(tmp_path / "kaist-home")
    os.environ["KAIST_PASSWORD_FOR_TEST"] = "hunter2"
    try:
        paths = resolve_paths()

        secret_store = _FakeSecretStore()
        auth = AuthService(paths, secret_store=secret_store)
        result = auth.store_email_otp_secret(password_env="KAIST_PASSWORD_FOR_TEST")
    finally:
        os.environ.pop("KAIST_PASSWORD_FOR_TEST", None)
        if old_home is None:
            os.environ.pop("KAIST_CLI_HOME", None)
        else:
            os.environ["KAIST_CLI_HOME"] = old_home

    assert secret_store.saved == ("student123", "hunter2")
    assert result.data["username"] == "student123"
    assert result.data["auth_strategy"] == "email_otp"


def test_auth_clear_email_otp_secret_uses_saved_username(tmp_path: Path) -> None:
    _write_config(tmp_path, auth_username="student123", auth_strategy="email_otp", otp_source="manual")
    old_home = os.environ.get("KAIST_CLI_HOME")
    os.environ["KAIST_CLI_HOME"] = str(tmp_path / "kaist-home")
    try:
        paths = resolve_paths()

        secret_store = _FakeSecretStore()
        auth = AuthService(paths, secret_store=secret_store)
        result = auth.clear_email_otp_secret()
    finally:
        if old_home is None:
            os.environ.pop("KAIST_CLI_HOME", None)
        else:
            os.environ["KAIST_CLI_HOME"] = old_home

    assert secret_store.deleted == "student123"
    assert result.data["username"] == "student123"


def test_auth_begin_refresh_requires_email_otp_strategy(tmp_path: Path) -> None:
    _write_config(tmp_path, auth_username="student123", auth_strategy="easy_login")
    old_home = os.environ.get("KAIST_CLI_HOME")
    os.environ["KAIST_CLI_HOME"] = str(tmp_path / "kaist-home")
    try:
        auth = AuthService(resolve_paths())
        with pytest.raises(CommandError) as exc_info:
            auth.begin_refresh()
    finally:
        if old_home is None:
            os.environ.pop("KAIST_CLI_HOME", None)
        else:
            os.environ["KAIST_CLI_HOME"] = old_home

    assert exc_info.value.code == "AUTH_FLOW_UNSUPPORTED"


def test_auth_begin_refresh_spawns_worker_and_returns_waiting_session(tmp_path: Path, monkeypatch) -> None:
    _write_config(tmp_path, auth_username="student123", auth_strategy="email_otp", otp_source="manual")
    old_home = os.environ.get("KAIST_CLI_HOME")
    os.environ["KAIST_CLI_HOME"] = str(tmp_path / "kaist-home")
    try:
        auth = AuthService(resolve_paths())
        spawned: list[str] = []

        class FakeWorker:
            pid = 43210

            def poll(self) -> None:
                return None

        def fake_spawn(session_id: str) -> FakeWorker:
            spawned.append(session_id)
            return FakeWorker()

        def fake_wait(*, session_id: str, wait_seconds: float, worker: FakeWorker) -> dict[str, object]:
            assert spawned == [session_id]
            assert wait_seconds == 45.0
            return {
                "session_id": session_id,
                "strategy": "email_otp",
                "otp_source": "manual",
                "started_at": "2026-04-06T00:00:00Z",
                "expires_at": "2026-04-06T00:10:00Z",
                "stage": "waiting_for_email_otp",
            }

        monkeypatch.setattr(auth, "_spawn_email_otp_worker", fake_spawn)
        monkeypatch.setattr(auth, "_wait_for_email_otp_worker_ready", fake_wait)
        result = auth.begin_refresh(wait_seconds=180.0)
        session = load_auth_session(resolve_paths())
    finally:
        if old_home is None:
            os.environ.pop("KAIST_CLI_HOME", None)
        else:
            os.environ["KAIST_CLI_HOME"] = old_home

    assert result.data["state"] == "waiting_for_email_otp"
    assert result.data["strategy"] == "email_otp"
    assert result.data["otp_source"] == "manual"
    assert spawned
    assert session is not None
    assert session["worker_pid"] == 43210


def test_email_otp_worker_command_uses_module_in_source_mode(tmp_path: Path) -> None:
    old_home = os.environ.get("KAIST_CLI_HOME")
    os.environ["KAIST_CLI_HOME"] = str(tmp_path / "kaist-home")
    try:
        auth = AuthService(resolve_paths())
        command = auth._email_otp_worker_command("abc123")
    finally:
        if old_home is None:
            os.environ.pop("KAIST_CLI_HOME", None)
        else:
            os.environ["KAIST_CLI_HOME"] = old_home

    assert command[:3] == [sys.executable, "-m", "kaist_cli.main"]
    assert command[-4:] == ["klms", "auth", "_worker-run", "abc123"]


def test_email_otp_worker_command_uses_frozen_binary_entrypoint(tmp_path: Path, monkeypatch) -> None:
    old_home = os.environ.get("KAIST_CLI_HOME")
    os.environ["KAIST_CLI_HOME"] = str(tmp_path / "kaist-home")
    try:
        monkeypatch.setattr(auth_module.sys, "executable", "/tmp/kaist", raising=False)
        monkeypatch.setattr(auth_module.sys, "frozen", True, raising=False)
        auth = AuthService(resolve_paths())
        command = auth._email_otp_worker_command("abc123")
    finally:
        if old_home is None:
            os.environ.pop("KAIST_CLI_HOME", None)
        else:
            os.environ["KAIST_CLI_HOME"] = old_home

    assert command == ["/tmp/kaist", "klms", "auth", "_worker-run", "abc123"]


def test_auth_cancel_refresh_clears_staged_session(tmp_path: Path) -> None:
    old_home = os.environ.get("KAIST_CLI_HOME")
    os.environ["KAIST_CLI_HOME"] = str(tmp_path / "kaist-home")
    try:
        paths = resolve_paths()
        save_auth_session(
            paths,
            {
                "session_id": "abc123",
                "strategy": "email_otp",
                "stage": "waiting_for_email_otp",
                "username": "student123",
                "started_at": "2026-04-06T00:00:00Z",
                "expires_at": "2099-04-06T00:10:00Z",
            },
        )
        auth = AuthService(paths)
        result = auth.cancel_refresh("abc123")
    finally:
        if old_home is None:
            os.environ.pop("KAIST_CLI_HOME", None)
        else:
            os.environ["KAIST_CLI_HOME"] = old_home

    assert result.data["state"] == "canceled"
    assert load_auth_session(paths) is None


def test_auth_cancel_refresh_routes_to_worker_when_present(tmp_path: Path, monkeypatch) -> None:
    old_home = os.environ.get("KAIST_CLI_HOME")
    os.environ["KAIST_CLI_HOME"] = str(tmp_path / "kaist-home")
    try:
        paths = resolve_paths()
        save_auth_session(
            paths,
            {
                "session_id": "abc123",
                "strategy": "email_otp",
                "stage": "waiting_for_email_otp",
                "username": "student123",
                "started_at": "2026-04-06T00:00:00Z",
                "expires_at": "2099-04-06T00:10:00Z",
                "worker_port": 43210,
                "worker_token": "secret",
            },
        )
        auth = AuthService(paths)

        def fake_send(*, payload: dict[str, object], action: str, timeout_seconds: float, otp_code: str | None = None) -> dict[str, object]:
            assert payload["session_id"] == "abc123"
            assert action == "cancel"
            assert otp_code is None
            return {"ok": True, "data": {"ok": True, "state": "canceled", "session_id": "abc123", "strategy": "email_otp"}}

        monkeypatch.setattr(auth, "_send_email_otp_worker_command", fake_send)
        result = auth.cancel_refresh("abc123")
    finally:
        if old_home is None:
            os.environ.pop("KAIST_CLI_HOME", None)
        else:
            os.environ["KAIST_CLI_HOME"] = old_home

    assert result.data["state"] == "canceled"


def test_clear_auth_session_removes_staged_storage_state(tmp_path: Path) -> None:
    old_home = os.environ.get("KAIST_CLI_HOME")
    os.environ["KAIST_CLI_HOME"] = str(tmp_path / "kaist-home")
    try:
        paths = resolve_paths()
        paths.auth_session_state_path.parent.mkdir(parents=True, exist_ok=True)
        paths.auth_session_state_path.write_text("{}", encoding="utf-8")
        clear_auth_session(paths)
    finally:
        if old_home is None:
            os.environ.pop("KAIST_CLI_HOME", None)
        else:
            os.environ["KAIST_CLI_HOME"] = old_home

    assert not paths.auth_session_state_path.exists()


def test_auth_complete_refresh_rejects_missing_session(tmp_path: Path) -> None:
    old_home = os.environ.get("KAIST_CLI_HOME")
    os.environ["KAIST_CLI_HOME"] = str(tmp_path / "kaist-home")
    try:
        auth = AuthService(resolve_paths())
        with pytest.raises(CommandError) as exc_info:
            auth.complete_refresh("missing123", otp="123456")
    finally:
        if old_home is None:
            os.environ.pop("KAIST_CLI_HOME", None)
        else:
            os.environ["KAIST_CLI_HOME"] = old_home

    assert exc_info.value.code == "AUTH_SESSION_MISSING"


def test_auth_complete_refresh_routes_otp_to_worker(tmp_path: Path, monkeypatch) -> None:
    old_home = os.environ.get("KAIST_CLI_HOME")
    os.environ["KAIST_CLI_HOME"] = str(tmp_path / "kaist-home")
    try:
        paths = resolve_paths()
        save_auth_session(
            paths,
            {
                "session_id": "abc123",
                "strategy": "email_otp",
                "stage": "waiting_for_email_otp",
                "username": "student123",
                "started_at": "2026-04-06T00:00:00Z",
                "expires_at": "2099-04-06T00:10:00Z",
                "worker_port": 43210,
                "worker_token": "secret",
            },
        )
        auth = AuthService(paths)

        def fake_send(*, payload: dict[str, object], action: str, timeout_seconds: float, otp_code: str | None = None) -> dict[str, object]:
            assert payload["session_id"] == "abc123"
            assert action == "submit_otp"
            assert otp_code == "123456"
            return {
                "ok": True,
                "data": {
                    "ok": True,
                    "state": "completed",
                    "login_strategy": "email_otp",
                    "username": "student123",
                },
            }

        monkeypatch.setattr(auth, "_send_email_otp_worker_command", fake_send)
        result = auth.complete_refresh("abc123", otp="123456")
    finally:
        if old_home is None:
            os.environ.pop("KAIST_CLI_HOME", None)
        else:
            os.environ["KAIST_CLI_HOME"] = old_home

    assert result.data["state"] == "completed"
    assert result.data["login_strategy"] == "email_otp"


def test_auth_status_surfaces_worker_snapshot_without_secret_fields(tmp_path: Path) -> None:
    old_home = os.environ.get("KAIST_CLI_HOME")
    os.environ["KAIST_CLI_HOME"] = str(tmp_path / "kaist-home")
    try:
        paths = resolve_paths()
        save_auth_session(
            paths,
            {
                "session_id": "abc123",
                "strategy": "email_otp",
                "stage": "waiting_for_email_otp",
                "username": "student123",
                "otp_source": "manual",
                "started_at": "2026-04-06T00:00:00Z",
                "expires_at": "2099-04-06T00:10:00Z",
                "challenge_url": "https://sso.kaist.ac.kr/auth/kaist/user/login/second/view",
                "worker_pid": os.getpid(),
                "worker_port": 43210,
                "worker_token": "secret",
            },
        )
    finally:
        if old_home is None:
            os.environ.pop("KAIST_CLI_HOME", None)
        else:
            os.environ["KAIST_CLI_HOME"] = old_home

    cp = run_cli(tmp_path, "--json", "klms", "auth", "status")
    assert cp.returncode == 0, cp.stderr
    payload = json.loads(cp.stdout)
    staged = payload["data"]["staged_auth_session"]
    assert staged["worker"]["pid"] == os.getpid()
    assert staged["worker"]["running"] is True
    assert "worker_token" not in staged
    assert "worker_port" not in staged


def test_auth_status_clears_expired_stuck_starting_session(tmp_path: Path) -> None:
    old_home = os.environ.get("KAIST_CLI_HOME")
    os.environ["KAIST_CLI_HOME"] = str(tmp_path / "kaist-home")
    try:
        paths = resolve_paths()
        save_auth_session(
            paths,
            {
                "session_id": "stale123",
                "strategy": "email_otp",
                "stage": "starting",
                "username": "student123",
                "started_at": "2026-04-06T00:00:00Z",
                "expires_at": "2026-04-06T00:10:00Z",
            },
        )
    finally:
        if old_home is None:
            os.environ.pop("KAIST_CLI_HOME", None)
        else:
            os.environ["KAIST_CLI_HOME"] = old_home

    cp = run_cli(tmp_path, "--json", "klms", "auth", "status")
    assert cp.returncode == 0, cp.stderr
    payload = json.loads(cp.stdout)
    assert payload["data"]["staged_auth_session"] is None


def test_wait_for_email_otp_worker_ready_includes_worker_log_tail(tmp_path: Path, monkeypatch) -> None:
    _write_config(tmp_path, auth_username="student123", auth_strategy="email_otp", otp_source="manual")
    old_home = os.environ.get("KAIST_CLI_HOME")
    os.environ["KAIST_CLI_HOME"] = str(tmp_path / "kaist-home")
    try:
        paths = resolve_paths()
        save_auth_session(
            paths,
            {
                "session_id": "abc123",
                "strategy": "email_otp",
                "stage": "starting",
                "username": "student123",
                "started_at": "2099-04-06T00:00:00Z",
                "expires_at": "2099-04-06T00:10:00Z",
                "worker_pid": 43210,
            },
        )
        paths.auth_worker_log_path.parent.mkdir(parents=True, exist_ok=True)
        paths.auth_worker_log_path.write_text("Traceback...\nRuntimeError: boom\n", encoding="utf-8")
        auth = AuthService(paths)
        monkeypatch.setattr(auth_otp_module, "_pid_is_running", lambda pid: True)
        monkeypatch.setattr(auth_module, "_pid_is_running", lambda pid: True)

        class FakeWorker:
            def poll(self) -> int:
                return 1

        with pytest.raises(CommandError) as exc_info:
            auth._wait_for_email_otp_worker_ready(session_id="abc123", wait_seconds=15.0, worker=FakeWorker())
    finally:
        if old_home is None:
            os.environ.pop("KAIST_CLI_HOME", None)
        else:
            os.environ["KAIST_CLI_HOME"] = old_home

    assert exc_info.value.code == "AUTH_FAILED"
    assert "RuntimeError: boom" in exc_info.value.message


def test_auth_complete_refresh_clears_stale_worker_session(tmp_path: Path, monkeypatch) -> None:
    old_home = os.environ.get("KAIST_CLI_HOME")
    os.environ["KAIST_CLI_HOME"] = str(tmp_path / "kaist-home")
    try:
        paths = resolve_paths()
        save_auth_session(
            paths,
            {
                "session_id": "abc123",
                "strategy": "email_otp",
                "stage": "waiting_for_email_otp",
                "username": "student123",
                "started_at": "2026-04-06T00:00:00Z",
                "expires_at": "2099-04-06T00:10:00Z",
                "worker_pid": 987654,
                "worker_port": 43210,
                "worker_token": "secret",
            },
        )
        auth = AuthService(paths)
        monkeypatch.setattr(auth_module.socket, "create_connection", lambda *args, **kwargs: (_ for _ in ()).throw(ConnectionRefusedError("refused")))
        monkeypatch.setattr(auth_otp_module, "_pid_is_running", lambda pid: False)
        with pytest.raises(CommandError) as exc_info:
            auth.complete_refresh("abc123", otp="123456")
        remaining = load_auth_session(paths)
    finally:
        if old_home is None:
            os.environ.pop("KAIST_CLI_HOME", None)
        else:
            os.environ["KAIST_CLI_HOME"] = old_home

    assert exc_info.value.code == "AUTH_SESSION_EXPIRED"
    assert remaining is None


def test_validate_email_otp_request_maps_invalid_code(tmp_path: Path) -> None:
    old_home = os.environ.get("KAIST_CLI_HOME")
    os.environ["KAIST_CLI_HOME"] = str(tmp_path / "kaist-home")
    try:
        auth = AuthService(resolve_paths())

        class FakeResponse:
            def json(self) -> dict[str, str]:
                return {"code": "E001"}

        class FakeRequest:
            def post(self, url: str, *, form: dict[str, str]) -> FakeResponse:  # type: ignore[no-untyped-def]
                assert url.endswith("/ajaxValidCrtfcNo")
                assert form == {"crtfc_no": "123456"}
                return FakeResponse()

        fake_context = SimpleNamespace(request=FakeRequest())
        with pytest.raises(CommandError) as exc_info:
            auth._validate_email_otp_request(context=fake_context, otp_code="123456")
    finally:
        if old_home is None:
            os.environ.pop("KAIST_CLI_HOME", None)
        else:
            os.environ["KAIST_CLI_HOME"] = old_home

    assert exc_info.value.code == "AUTH_OTP_INVALID"


def test_validate_email_otp_request_accepts_success_code(tmp_path: Path) -> None:
    old_home = os.environ.get("KAIST_CLI_HOME")
    os.environ["KAIST_CLI_HOME"] = str(tmp_path / "kaist-home")
    try:
        auth = AuthService(resolve_paths())

        class FakeResponse:
            def json(self) -> dict[str, str]:
                return {"code": "SS0001"}

        class FakeRequest:
            def post(self, url: str, *, form: dict[str, str]) -> FakeResponse:  # type: ignore[no-untyped-def]
                assert url.endswith("/ajaxValidCrtfcNo")
                assert form == {"crtfc_no": "654321"}
                return FakeResponse()

        fake_context = SimpleNamespace(request=FakeRequest())
        code = auth._validate_email_otp_request(context=fake_context, otp_code="654321")
    finally:
        if old_home is None:
            os.environ.pop("KAIST_CLI_HOME", None)
        else:
            os.environ["KAIST_CLI_HOME"] = old_home

    assert code == "SS0001"


def test_validate_email_otp_request_accepts_device_registration_code(tmp_path: Path) -> None:
    old_home = os.environ.get("KAIST_CLI_HOME")
    os.environ["KAIST_CLI_HOME"] = str(tmp_path / "kaist-home")
    try:
        auth = AuthService(resolve_paths())

        class FakeResponse:
            def json(self) -> dict[str, str]:
                return {"code": "SS0099"}

        class FakeRequest:
            def post(self, url: str, *, form: dict[str, str]) -> FakeResponse:  # type: ignore[no-untyped-def]
                assert url.endswith("/ajaxValidCrtfcNo")
                assert form == {"crtfc_no": "777777"}
                return FakeResponse()

        fake_context = SimpleNamespace(request=FakeRequest())
        code = auth._validate_email_otp_request(context=fake_context, otp_code="777777")
    finally:
        if old_home is None:
            os.environ.pop("KAIST_CLI_HOME", None)
        else:
            os.environ["KAIST_CLI_HOME"] = old_home

    assert code == "SS0099"


def test_keychain_secret_store_shells_out_to_security_cli(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, *, check, capture_output, text):  # type: ignore[no-untyped-def]
        calls.append(list(cmd))
        if "find-generic-password" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="hunter2\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(secrets_module.sys, "platform", "darwin")
    monkeypatch.setattr(secrets_module.subprocess, "run", fake_run)

    store = secrets_module.KeychainSecretStore(service="kaist-cli.test")
    store.store_email_otp_password(username="student123", password="hunter2")
    password = store.load_email_otp_password(username="student123")
    store.delete_email_otp_password(username="student123")

    assert password == "hunter2"
    assert calls == [
        ["security", "add-generic-password", "-U", "-s", "kaist-cli.test", "-a", "student123", "-w", "hunter2"],
        ["security", "find-generic-password", "-s", "kaist-cli.test", "-a", "student123", "-w"],
        ["security", "delete-generic-password", "-s", "kaist-cli.test", "-a", "student123"],
    ]


def test_run_authenticated_configures_playwright_env_before_launch(tmp_path: Path, monkeypatch) -> None:
    old_home = os.environ.get("KAIST_CLI_HOME")
    os.environ["KAIST_CLI_HOME"] = str(tmp_path / "kaist-home")
    try:
        _write_config(tmp_path)
        _write_storage_state(tmp_path)
        paths = resolve_paths()
        auth = AuthService(paths)
        config = load_config(paths)
        calls: list[str] = []

        def fake_configure(paths_arg):  # type: ignore[no-untyped-def]
            calls.append("configure")
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(paths_arg.playwright_browsers_dir)
            return paths_arg.playwright_browsers_dir

        class FakePlaywrightContext:
            def __enter__(self):  # type: ignore[no-untyped-def]
                calls.append("sync_playwright")
                assert os.environ.get("PLAYWRIGHT_BROWSERS_PATH") == str(paths.playwright_browsers_dir)
                return SimpleNamespace()

            def __exit__(self, exc_type, exc, tb):  # type: ignore[no-untyped-def]
                return False

        monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
        monkeypatch.setattr(auth_module, "configure_playwright_env", fake_configure)
        monkeypatch.setattr("playwright.sync_api.sync_playwright", lambda: FakePlaywrightContext())
        monkeypatch.setattr(auth_module, "_launch_chromium_persistent_context_sync", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("stop")))
        monkeypatch.setattr(auth_module, "_launch_chromium_browser_sync", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("stop")))

        with pytest.raises(CommandError) as exc_info:
            auth.run_authenticated(
                config=config,
                headless=True,
                accept_downloads=False,
                timeout_seconds=1.0,
                callback=lambda context, auth_mode: None,
            )
    finally:
        if old_home is None:
            os.environ.pop("KAIST_CLI_HOME", None)
        else:
            os.environ["KAIST_CLI_HOME"] = old_home

    assert exc_info.value.code == "AUTH_CHECK_UNAVAILABLE"
    assert calls[:2] == ["configure", "sync_playwright"]


def test_run_authenticated_reports_dashboard_check_failure_as_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KAIST_CLI_HOME", str(tmp_path / "kaist-home"))
    _write_config(tmp_path)
    _write_storage_state(tmp_path)
    paths = resolve_paths()
    auth = AuthService(paths)

    class FakePlaywrightContext:
        def __enter__(self) -> object:
            return object()

        def __exit__(self, exc_type, exc, tb) -> bool:  # type: ignore[no-untyped-def]
            return False

    class FakeContext:
        def close(self) -> None:
            return None

    class FakeBrowser:
        def new_context(self, **kwargs):  # type: ignore[no-untyped-def]
            return FakeContext()

        def close(self) -> None:
            return None

    monkeypatch.setattr(auth_module, "configure_playwright_env", lambda paths: paths.playwright_browsers_dir)
    monkeypatch.setattr("playwright.sync_api.sync_playwright", lambda: FakePlaywrightContext())
    monkeypatch.setattr(auth_module, "_launch_chromium_browser_sync", lambda *args, **kwargs: FakeBrowser())
    monkeypatch.setattr(
        auth,
        "_context_dashboard_state",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("dashboard timeout")),
    )

    with pytest.raises(CommandError) as exc_info:
        auth.run_authenticated(
            config=load_config(paths),
            headless=True,
            accept_downloads=False,
            timeout_seconds=1.0,
            callback=lambda context, auth_mode: None,
        )

    assert exc_info.value.code == "AUTH_CHECK_UNAVAILABLE"
    assert "check_error=dashboard timeout" in exc_info.value.message


def test_reusable_profile_validation_records_auth_verification(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KAIST_CLI_HOME", str(tmp_path / "kaist-home"))
    _write_config(tmp_path)
    paths = resolve_paths()
    (paths.profile_dir / "Cookies").parent.mkdir(parents=True, exist_ok=True)
    (paths.profile_dir / "Cookies").write_text("session", encoding="utf-8")
    auth = AuthService(paths)

    class FakeContext:
        def close(self) -> None:
            return None

    monkeypatch.setattr(auth_module, "_launch_chromium_persistent_context_sync", lambda *args, **kwargs: FakeContext())
    monkeypatch.setattr(
        auth,
        "_context_dashboard_state",
        lambda *args, **kwargs: {"authenticated": True, "final_url": "https://klms.kaist.ac.kr/my/"},
    )

    auth._assert_saved_auth_session_reusable_with_playwright(
        playwright=object(),
        config=load_config(paths),
        timeout_seconds=1.0,
    )

    assert auth_module.load_auth_verified(paths) is not None


def test_reusable_storage_state_validation_records_auth_verification(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KAIST_CLI_HOME", str(tmp_path / "kaist-home"))
    _write_config(tmp_path)
    _write_storage_state(tmp_path)
    paths = resolve_paths()
    auth = AuthService(paths)

    class FakeContext:
        def close(self) -> None:
            return None

    class FakeBrowser:
        def new_context(self, **kwargs):  # type: ignore[no-untyped-def]
            return FakeContext()

        def close(self) -> None:
            return None

    monkeypatch.setattr(auth_module, "_launch_chromium_browser_sync", lambda *args, **kwargs: FakeBrowser())
    monkeypatch.setattr(
        auth,
        "_context_dashboard_state",
        lambda *args, **kwargs: {"authenticated": True, "final_url": "https://klms.kaist.ac.kr/my/"},
    )

    auth._assert_saved_auth_session_reusable_with_playwright(
        playwright=object(),
        config=load_config(paths),
        timeout_seconds=1.0,
    )

    assert auth_module.load_auth_verified(paths) is not None


def test_email_otp_storage_state_validation_records_auth_verification(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KAIST_CLI_HOME", str(tmp_path / "kaist-home"))
    _write_config(tmp_path)
    _write_storage_state(tmp_path)
    paths = resolve_paths()
    auth = AuthService(paths)

    class FakeContext:
        def close(self) -> None:
            return None

    class FakeBrowser:
        def new_context(self, **kwargs):  # type: ignore[no-untyped-def]
            return FakeContext()

    monkeypatch.setattr(
        auth,
        "_context_dashboard_state",
        lambda *args, **kwargs: {"authenticated": True, "final_url": "https://klms.kaist.ac.kr/my/"},
    )

    auth._assert_storage_state_reusable(
        browser=FakeBrowser(),
        config=load_config(paths),
        timeout_seconds=1.0,
    )

    assert auth_module.load_auth_verified(paths) is not None


def test_run_authenticated_raises_concurrent_access_when_profile_lock_is_held(tmp_path: Path) -> None:
    old_home = os.environ.get("KAIST_CLI_HOME")
    os.environ["KAIST_CLI_HOME"] = str(tmp_path / "kaist-home")
    try:
        _write_config(tmp_path)
        paths = resolve_paths()
        auth = AuthService(paths)
        config = load_config(paths)
        paths.profile_dir.mkdir(parents=True, exist_ok=True)
        (paths.profile_dir / "session-cookie").write_text("present", encoding="utf-8")

        lock_handle = open(paths.profile_lock_path, "a+b")
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with pytest.raises(CommandError) as exc_info:
                auth.run_authenticated(
                    config=config,
                    headless=True,
                    accept_downloads=False,
                    timeout_seconds=1.0,
                    callback=lambda context, auth_mode: None,
                )
        finally:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
            lock_handle.close()
    finally:
        if old_home is None:
            os.environ.pop("KAIST_CLI_HOME", None)
        else:
            os.environ["KAIST_CLI_HOME"] = old_home

    assert exc_info.value.code == "CONCURRENT_ACCESS"
    assert exc_info.value.retryable is True


def test_command_error_survives_profile_lock_context(tmp_path: Path) -> None:
    old_home = os.environ.get("KAIST_CLI_HOME")
    os.environ["KAIST_CLI_HOME"] = str(tmp_path / "kaist-home")
    try:
        paths = resolve_paths()
        with pytest.raises(CommandError) as exc_info:
            with auth_module._hold_profile_lock(paths):
                raise CommandError(code="AUTH_FLOW_UNSUPPORTED", message="broken")
    finally:
        if old_home is None:
            os.environ.pop("KAIST_CLI_HOME", None)
        else:
            os.environ["KAIST_CLI_HOME"] = old_home

    assert exc_info.value.code == "AUTH_FLOW_UNSUPPORTED"
    assert exc_info.value.message == "broken"


def test_v2_json_envelope_reports_concurrent_access_when_profile_lock_is_held(tmp_path: Path) -> None:
    old_home = os.environ.get("KAIST_CLI_HOME")
    os.environ["KAIST_CLI_HOME"] = str(tmp_path / "kaist-home")
    try:
        _write_config(tmp_path)
        paths = resolve_paths()
        paths.profile_dir.mkdir(parents=True, exist_ok=True)
        (paths.profile_dir / "session-cookie").write_text("present", encoding="utf-8")

        lock_handle = open(paths.profile_lock_path, "a+b")
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        cp = run_cli(tmp_path, "--json", "klms", "courses", "list")
    finally:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        lock_handle.close()
        if old_home is None:
            os.environ.pop("KAIST_CLI_HOME", None)
        else:
            os.environ["KAIST_CLI_HOME"] = old_home

    assert cp.returncode == 20
    payload = json.loads(cp.stdout)
    assert payload["ok"] is False
    assert payload["schema"] == "kaist.klms.courses.list.v1"
    assert payload["error"]["code"] == "CONCURRENT_ACCESS"
    assert payload["error"]["retryable"] is True


def test_custom_login_url_detection_is_enabled() -> None:
    assert looks_login_url("https://klms.kaist.ac.kr/local/applogin/result_login_json.php?userid=test")


def test_extract_sso_login_view_url_from_klms_login_shell() -> None:
    html = """
    <html><body>
      <a href="https://sso.kaist.ac.kr/auth/kaist/user/login/view?agt_id=kaist-prod-klms">Log in</a>
    </body></html>
    """
    assert _extract_sso_login_view_url("https://klms.kaist.ac.kr/login/ssologin.php", html) == (
        "https://sso.kaist.ac.kr/auth/kaist/user/login/view?agt_id=kaist-prod-klms"
    )


def test_extract_easy_login_number_and_error_message() -> None:
    html = """
    <html><body>
      <div id="authNumber">Login Number: 482913</div>
      <label id="mfaResultMsg">Easy Login app is not registered.</label>
    </body></html>
    """
    assert _extract_easy_login_number(html) == "482913"
    assert _extract_easy_login_error_message(html) == "Easy Login app is not registered."


def test_submit_password_login_targets_id_pw_tab_selectors() -> None:
    calls: list[tuple[str, list[str]]] = []

    class FakePage:
        def evaluate(self, script: str, payload: list[str]) -> bool:  # type: ignore[no-untyped-def]
            calls.append((script, payload))
            return True

    assert _submit_password_login(FakePage(), username="student123", password="hunter2") is True
    assert len(calls) == 1
    script, payload = calls[0]
    assert payload == ["student123", "hunter2"]
    assert isinstance(script, str) and script.strip()


def test_request_email_otp_delivery_targets_email_button() -> None:
    calls: list[str] = []

    class FakePage:
        def evaluate(self, script: str) -> bool:  # type: ignore[no-untyped-def]
            calls.append(script)
            return True

    assert _request_email_otp_delivery(FakePage()) is True
    assert len(calls) == 1
    assert isinstance(calls[0], str) and calls[0].strip()


def test_submit_email_otp_code_reenables_second_step_controls() -> None:
    calls: list[tuple[str, str]] = []

    class FakePage:
        def evaluate(self, script: str, payload: str) -> bool:  # type: ignore[no-untyped-def]
            calls.append((script, payload))
            return True

    assert auth_module._submit_email_otp_code(FakePage(), otp="123456") is True
    assert len(calls) == 1
    script, payload = calls[0]
    assert payload == "123456"
    assert "window.send_flag = true" in script
    assert "classList.remove('disable')" in script


def test_extract_easy_login_number_from_verification_widget() -> None:
    html = """
    <html><body>
      <div class="auth_number">
        <em></em>
        <div class="nember_wrap">
          <span>5</span>
          <span>0</span>
        </div>
        <div style="font-size:0;" class="sr-only" aria-hidden="false" tabindex="0">50</div>
      </div>
    </body></html>
    """
    assert _extract_easy_login_number(html) == "50"


def test_extract_easy_login_number_ignores_countdown_digits() -> None:
    html = """
    <html><body>
      <div class="auth_contents_wrap">
        <div class="auth_number">
          <div class="nember_wrap">
            <span>1</span>
            <span>4</span>
          </div>
        </div>
        <div class="auth_time_wrap">
          <strong id="countdown">88</strong>
        </div>
      </div>
    </body></html>
    """
    assert _extract_easy_login_number(html) == "14"


def test_should_update_easy_login_number_rejects_mutated_longer_code() -> None:
    assert _should_update_easy_login_number(previous="15", current="1488") is False
    assert _should_update_easy_login_number(previous="15", current="28") is True


def test_wait_for_easy_login_init_accepts_verification_page_without_second_redirect(tmp_path: Path) -> None:
    class FakePage:
        def __init__(self) -> None:
            self.url = "https://sso.kaist.ac.kr/auth/twofactor/mfa/login2Factor"
            self.html = _read_fixture("kaist_sso_login2factor.html")

        def content(self) -> str:
            return self.html

        def wait_for_timeout(self, ms: int) -> None:  # noqa: ARG002
            raise AssertionError("verification page should be accepted immediately")

    old_home = os.environ.get("KAIST_CLI_HOME")
    os.environ["KAIST_CLI_HOME"] = str(tmp_path / "kaist-home")
    try:
        auth = AuthService(resolve_paths())
        html = auth._wait_for_easy_login_init(FakePage(), timeout_seconds=1.0)
    finally:
        if old_home is None:
            os.environ.pop("KAIST_CLI_HOME", None)
        else:
            os.environ["KAIST_CLI_HOME"] = old_home

    assert _extract_easy_login_number(html) == "50"


def test_wait_for_easy_login_approval_succeeds_from_mfa_and_policy_responses(tmp_path: Path, monkeypatch) -> None:
    class FakePage:
        def __init__(self) -> None:
            self.url = "https://sso.kaist.ac.kr/auth/twofactor/mfa/login2Factor"
            self.html = _read_fixture("kaist_sso_login2factor.html")
            self.submitted = False
            self.loop_count = 0

        def content(self) -> str:
            return self.html

        def wait_for_timeout(self, ms: int) -> None:  # noqa: ARG002
            self.loop_count += 1
            if self.loop_count == 1:
                signals.latest_mfa_payload = {"result": True}
            elif self.loop_count == 2:
                signals.latest_policy_payload = {"code": "SS0001"}

        def evaluate(self, script: str) -> bool:  # noqa: ARG002
            self.submitted = True
            self.url = "https://sso.kaist.ac.kr/auth/user/login/link"
            return True

    old_home = os.environ.get("KAIST_CLI_HOME")
    os.environ["KAIST_CLI_HOME"] = str(tmp_path / "kaist-home")
    try:
        _write_config(tmp_path)
        paths = resolve_paths()
        config = load_config(paths)
        auth = AuthService(paths)
        page = FakePage()
        signals = _EasyLoginSignals()
        persisted: list[bool] = []

        tick = {"value": 0.0}

        def fake_monotonic() -> float:
            tick["value"] += 0.6
            return tick["value"]

        monkeypatch.setattr(auth_sso_module, "EASY_LOGIN_POLL_SECONDS", 0.0)
        monkeypatch.setattr(auth_module.time, "monotonic", fake_monotonic)
        monkeypatch.setattr(auth, "_persist_context_state", lambda context: persisted.append(True))
        monkeypatch.setattr(
            auth,
            "_context_dashboard_state",
            lambda context, *, config, timeout_ms: {  # type: ignore[no-untyped-def]
                "authenticated": page.submitted,
                "final_url": config.base_url.rstrip("/") + config.dashboard_path,
                "html": "<html></html>",
            },
        )

        result = auth._wait_for_easy_login_approval(
            page=page,
            context=object(),
            config=config,
            username="student123",
            wait_seconds=20.0,
            login_number="50",
            signals=signals,
        )
    finally:
        if old_home is None:
            os.environ.pop("KAIST_CLI_HOME", None)
        else:
            os.environ["KAIST_CLI_HOME"] = old_home

    assert persisted == [True]
    assert result.data["login_strategy"] == "sso_easy_login"
    assert result.data["login_number"] == "50"


def test_wait_for_easy_login_approval_fails_fast_on_canceled_request(tmp_path: Path, monkeypatch) -> None:
    class FakePage:
        def __init__(self) -> None:
            self.url = "https://sso.kaist.ac.kr/auth/twofactor/mfa/login2Factor"
            self.html = _read_fixture("kaist_sso_login2factor.html")

        def content(self) -> str:
            return self.html

        def wait_for_timeout(self, ms: int) -> None:  # noqa: ARG002
            raise AssertionError("should fail before waiting")

    old_home = os.environ.get("KAIST_CLI_HOME")
    os.environ["KAIST_CLI_HOME"] = str(tmp_path / "kaist-home")
    try:
        _write_config(tmp_path)
        paths = resolve_paths()
        config = load_config(paths)
        auth = AuthService(paths)
        signals = _EasyLoginSignals(latest_mfa_payload={"result": False, "error_code": "ESY023"})
        monkeypatch.setattr(auth_sso_module, "EASY_LOGIN_POLL_SECONDS", 0.0)
        with pytest.raises(CommandError) as excinfo:
            auth._wait_for_easy_login_approval(
                page=FakePage(),
                context=object(),
                config=config,
                username="student123",
                wait_seconds=20.0,
                login_number="50",
                signals=signals,
            )
    finally:
        if old_home is None:
            os.environ.pop("KAIST_CLI_HOME", None)
        else:
            os.environ["KAIST_CLI_HOME"] = old_home

    error = excinfo.value
    assert getattr(error, "code", None) == "AUTH_FAILED"
    assert "canceled" in str(error).lower()


def test_wait_for_easy_login_approval_times_out_on_repeated_waiting_state(tmp_path: Path, monkeypatch) -> None:
    class FakePage:
        def __init__(self) -> None:
            self.url = "https://sso.kaist.ac.kr/auth/twofactor/mfa/login2Factor"
            self.html = _read_fixture("kaist_sso_login2factor.html")

        def content(self) -> str:
            return self.html

        def wait_for_timeout(self, ms: int) -> None:  # noqa: ARG002
            return None

    old_home = os.environ.get("KAIST_CLI_HOME")
    os.environ["KAIST_CLI_HOME"] = str(tmp_path / "kaist-home")
    try:
        _write_config(tmp_path)
        paths = resolve_paths()
        config = load_config(paths)
        auth = AuthService(paths)
        signals = _EasyLoginSignals(latest_mfa_payload={"result": False, "error_code": "ESY020"})
        tick = {"value": 0.0}

        def fake_monotonic() -> float:
            tick["value"] += 5.0
            return tick["value"]

        monkeypatch.setattr(auth_sso_module, "EASY_LOGIN_POLL_SECONDS", 0.0)
        monkeypatch.setattr(auth_module.time, "monotonic", fake_monotonic)
        monkeypatch.setattr(
            auth,
            "_context_dashboard_state",
            lambda context, *, config, timeout_ms: {  # type: ignore[no-untyped-def]
                "authenticated": False,
                "final_url": config.base_url.rstrip("/") + config.dashboard_path,
                "html": "<html></html>",
            },
        )
        with pytest.raises(CommandError) as excinfo:
            auth._wait_for_easy_login_approval(
                page=FakePage(),
                context=object(),
                config=config,
                username="student123",
                wait_seconds=15.0,
                login_number="50",
                signals=signals,
            )
    finally:
        if old_home is None:
            os.environ.pop("KAIST_CLI_HOME", None)
        else:
            os.environ["KAIST_CLI_HOME"] = old_home

    error = excinfo.value
    assert getattr(error, "code", None) == "AUTH_TIMEOUT"


def test_wait_for_easy_login_approval_keeps_original_number_when_extractor_mutates(tmp_path: Path, monkeypatch) -> None:
    class FakePage:
        def __init__(self) -> None:
            self.url = "https://sso.kaist.ac.kr/auth/twofactor/mfa/login2Factor"
            self.html = _read_fixture("kaist_sso_login2factor.html")

        def content(self) -> str:
            return self.html

        def wait_for_timeout(self, ms: int) -> None:  # noqa: ARG002
            return None

    old_home = os.environ.get("KAIST_CLI_HOME")
    os.environ["KAIST_CLI_HOME"] = str(tmp_path / "kaist-home")
    try:
        _write_config(tmp_path)
        paths = resolve_paths()
        config = load_config(paths)
        auth = AuthService(paths)
        signals = _EasyLoginSignals(latest_mfa_payload={"result": False, "error_code": "ESY020"})
        tick = {"value": 0.0}

        def fake_monotonic() -> float:
            tick["value"] += 5.0
            return tick["value"]

        monkeypatch.setattr(auth_sso_module, "EASY_LOGIN_POLL_SECONDS", 0.0)
        monkeypatch.setattr(auth_module.time, "monotonic", fake_monotonic)
        monkeypatch.setattr(auth_sso_module, "_extract_easy_login_number", lambda html: "1488")
        monkeypatch.setattr(
            auth,
            "_context_dashboard_state",
            lambda context, *, config, timeout_ms: {  # type: ignore[no-untyped-def]
                "authenticated": False,
                "final_url": config.base_url.rstrip("/") + config.dashboard_path,
                "html": "<html></html>",
            },
        )
        with pytest.raises(CommandError) as excinfo:
            auth._wait_for_easy_login_approval(
                page=FakePage(),
                context=object(),
                config=config,
                username="student123",
                wait_seconds=15.0,
                login_number="15",
                signals=signals,
            )
    finally:
        if old_home is None:
            os.environ.pop("KAIST_CLI_HOME", None)
        else:
            os.environ["KAIST_CLI_HOME"] = old_home

    assert "Login number: 15." in str(excinfo.value)


def test_wait_for_easy_login_approval_completes_device_registration_and_succeeds(tmp_path: Path, monkeypatch) -> None:
    class FakePage:
        def __init__(self) -> None:
            self.url = "https://sso.kaist.ac.kr/auth/kaist/user/device/view"
            self.registered = False

        def wait_for_timeout(self, ms: int) -> None:  # noqa: ARG002
            return None

        def evaluate(self, script: str) -> bool:  # noqa: ARG002
            self.registered = True
            self.url = "https://sso.kaist.ac.kr/auth/kaist/user/device/login"
            return True

    old_home = os.environ.get("KAIST_CLI_HOME")
    os.environ["KAIST_CLI_HOME"] = str(tmp_path / "kaist-home")
    try:
        _write_config(tmp_path)
        paths = resolve_paths()
        config = load_config(paths)
        auth = AuthService(paths)
        page = FakePage()
        signals = _EasyLoginSignals(latest_mfa_payload={"result": True, "error_code": ""})
        monkeypatch.setattr(auth_sso_module, "EASY_LOGIN_POLL_SECONDS", 0.0)
        monkeypatch.setattr(auth, "_persist_context_state", lambda context: None)
        monkeypatch.setattr(
            auth,
            "_context_dashboard_state",
            lambda context, *, config, timeout_ms: {  # type: ignore[no-untyped-def]
                "authenticated": page.registered,
                "final_url": config.base_url.rstrip("/") + config.dashboard_path,
                "html": "<html></html>",
            },
        )
        result = auth._wait_for_easy_login_approval(
            page=page,
            context=object(),
            config=config,
            username="student123",
            wait_seconds=20.0,
            login_number="50",
            signals=signals,
        )
    finally:
        if old_home is None:
            os.environ.pop("KAIST_CLI_HOME", None)
        else:
            os.environ["KAIST_CLI_HOME"] = old_home

    assert result.data["username"] == "student123"
    assert page.registered is True


def test_wait_for_easy_login_approval_errors_if_device_registration_fails(tmp_path: Path, monkeypatch) -> None:
    class FakePage:
        def __init__(self) -> None:
            self.url = "https://sso.kaist.ac.kr/auth/kaist/user/device/view"

        def wait_for_timeout(self, ms: int) -> None:  # noqa: ARG002
            raise AssertionError("device-registration path should fail before waiting again")

        def evaluate(self, script: str) -> bool:  # noqa: ARG002
            raise RuntimeError("js disabled")

        def goto(self, url: str, **kwargs: Any) -> None:  # noqa: ARG002
            raise RuntimeError("navigation blocked")

    old_home = os.environ.get("KAIST_CLI_HOME")
    os.environ["KAIST_CLI_HOME"] = str(tmp_path / "kaist-home")
    try:
        _write_config(tmp_path)
        paths = resolve_paths()
        config = load_config(paths)
        auth = AuthService(paths)
        signals = _EasyLoginSignals(latest_mfa_payload={"result": True, "error_code": ""})
        monkeypatch.setattr(auth_sso_module, "EASY_LOGIN_POLL_SECONDS", 0.0)
        with pytest.raises(CommandError) as excinfo:
            auth._wait_for_easy_login_approval(
                page=FakePage(),
                context=object(),
                config=config,
                username="student123",
                wait_seconds=20.0,
                login_number="50",
                signals=signals,
            )
    finally:
        if old_home is None:
            os.environ.pop("KAIST_CLI_HOME", None)
        else:
            os.environ["KAIST_CLI_HOME"] = old_home

    error = excinfo.value
    assert getattr(error, "code", None) == "AUTH_FLOW_UNSUPPORTED"
    assert "device registration" in str(error).lower()


def test_wait_for_easy_login_approval_tolerates_page_content_error_during_navigation(tmp_path: Path, monkeypatch) -> None:
    class FakePage:
        def __init__(self) -> None:
            self.url = "https://sso.kaist.ac.kr/auth/twofactor/mfa/login2Factor"
            self.html = _read_fixture("kaist_sso_login2factor.html")
            self.submitted = False
            self.loop_count = 0

        def content(self) -> str:
            if self.submitted:
                raise RuntimeError("page is navigating")
            return self.html

        def wait_for_timeout(self, ms: int) -> None:  # noqa: ARG002
            self.loop_count += 1
            if self.loop_count == 1:
                signals.latest_mfa_payload = {"result": True}
                signals.latest_policy_payload = {"code": "SS0001"}

        def evaluate(self, script: str) -> bool:  # noqa: ARG002
            self.submitted = True
            self.url = "https://sso.kaist.ac.kr/auth/user/login/link"
            return True

    old_home = os.environ.get("KAIST_CLI_HOME")
    os.environ["KAIST_CLI_HOME"] = str(tmp_path / "kaist-home")
    try:
        _write_config(tmp_path)
        paths = resolve_paths()
        config = load_config(paths)
        auth = AuthService(paths)
        page = FakePage()
        signals = _EasyLoginSignals()
        tick = {"value": 0.0}

        def fake_monotonic() -> float:
            tick["value"] += 0.7
            return tick["value"]

        monkeypatch.setattr(auth_sso_module, "EASY_LOGIN_POLL_SECONDS", 0.0)
        monkeypatch.setattr(auth_module.time, "monotonic", fake_monotonic)
        monkeypatch.setattr(auth, "_persist_context_state", lambda context: None)
        monkeypatch.setattr(
            auth,
            "_context_dashboard_state",
            lambda context, *, config, timeout_ms: {  # type: ignore[no-untyped-def]
                "authenticated": page.submitted,
                "final_url": config.base_url.rstrip("/") + config.dashboard_path,
                "html": "<html></html>",
            },
        )

        result = auth._wait_for_easy_login_approval(
            page=page,
            context=object(),
            config=config,
            username="student123",
            wait_seconds=20.0,
            login_number="50",
            signals=signals,
        )
    finally:
        if old_home is None:
            os.environ.pop("KAIST_CLI_HOME", None)
        else:
            os.environ["KAIST_CLI_HOME"] = old_home

    assert result.data["username"] == "student123"


def test_wait_for_easy_login_approval_succeeds_from_authenticated_context_page(tmp_path: Path, monkeypatch) -> None:
    class ClosedSsoPage:
        @property
        def url(self) -> str:
            raise RuntimeError("page closed")

        def content(self) -> str:
            raise RuntimeError("page closed")

        def wait_for_timeout(self, ms: int) -> None:  # noqa: ARG002
            raise RuntimeError("page closed")

    class AuthenticatedKlmsPage:
        def __init__(self) -> None:
            self.url = "https://klms.kaist.ac.kr/my/"

        def content(self) -> str:
            return "<html><body>dashboard</body></html>"

    class FakeContext:
        def __init__(self) -> None:
            self.pages = [ClosedSsoPage(), AuthenticatedKlmsPage()]

    old_home = os.environ.get("KAIST_CLI_HOME")
    os.environ["KAIST_CLI_HOME"] = str(tmp_path / "kaist-home")
    try:
        _write_config(tmp_path)
        paths = resolve_paths()
        config = load_config(paths)
        auth = AuthService(paths)
        persisted: list[bool] = []
        monkeypatch.setattr(auth, "_persist_context_state", lambda context: persisted.append(True))
        result = auth._wait_for_easy_login_approval(
            page=ClosedSsoPage(),
            context=FakeContext(),
            config=config,
            username="student123",
            wait_seconds=20.0,
            login_number="50",
            signals=_EasyLoginSignals(latest_mfa_payload={"result": True}),
        )
    finally:
        if old_home is None:
            os.environ.pop("KAIST_CLI_HOME", None)
        else:
            os.environ["KAIST_CLI_HOME"] = old_home

    assert persisted == [True]
    assert result.data["username"] == "student123"


def test_easy_login_registers_response_listener_on_context(tmp_path: Path, monkeypatch) -> None:
    import playwright.sync_api as playwright_sync_api  # type: ignore[import-untyped]

    class FakePage:
        def __init__(self) -> None:
            self.url = "https://klms.kaist.ac.kr/"

        def goto(self, url: str, **kwargs: Any) -> None:  # noqa: ARG002
            self.url = url

        def fill(self, selector: str, value: str) -> None:  # noqa: ARG002
            return None

        def click(self, selector: str) -> None:  # noqa: ARG002
            return None

    class FakeContext:
        def __init__(self) -> None:
            self.page = FakePage()
            self.handlers: dict[str, Any] = {}

        def new_page(self) -> FakePage:
            return self.page

        def on(self, event: str, handler: Any) -> None:
            self.handlers[event] = handler

        def close(self) -> None:
            return None

    class FakePlaywrightContextManager:
        def __enter__(self) -> object:
            return object()

        def __exit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
            return None

    old_home = os.environ.get("KAIST_CLI_HOME")
    os.environ["KAIST_CLI_HOME"] = str(tmp_path / "kaist-home")
    try:
        _write_config(tmp_path)
        paths = resolve_paths()
        config = load_config(paths)
        auth = AuthService(paths)
        fake_context = FakeContext()

        monkeypatch.setattr(playwright_sync_api, "sync_playwright", lambda: FakePlaywrightContextManager())
        monkeypatch.setattr(
            auth_sso_module,
            "_launch_chromium_persistent_context_sync",
            lambda *args, **kwargs: fake_context,  # noqa: ARG005
        )
        monkeypatch.setattr(
            auth_sso_module,
            "_extract_sso_login_view_url",
            lambda current_url, html: "https://sso.kaist.ac.kr/auth/kaist/user/login/view",  # noqa: ARG005
        )
        monkeypatch.setattr(auth, "_wait_for_easy_login_init", lambda page, timeout_seconds: _read_fixture("kaist_sso_login2factor.html"))  # noqa: ARG005
        monkeypatch.setattr(
            auth,
            "_wait_for_easy_login_approval",
            lambda **kwargs: CommandResult(  # type: ignore[misc]
                data={"ok": True, "signals_seen": kwargs["signals"] is not None},
                source="browser",
                capability="full",
            ),
        )
        monkeypatch.setattr(auth, "_assert_saved_auth_session_reusable_with_playwright", lambda **kwargs: None)

        result = auth._easy_login(config=config, username="student123", wait_seconds=30.0)
    finally:
        if old_home is None:
            os.environ.pop("KAIST_CLI_HOME", None)
        else:
            os.environ["KAIST_CLI_HOME"] = old_home

    assert result.data["ok"] is True
    assert "response" in fake_context.handlers


def test_manual_login_fails_early_without_interactive_stdin(tmp_path: Path, monkeypatch) -> None:
    class FakeStdin:
        def isatty(self) -> bool:
            return False

    old_home = os.environ.get("KAIST_CLI_HOME")
    os.environ["KAIST_CLI_HOME"] = str(tmp_path / "kaist-home")
    try:
        _write_config(tmp_path)
        paths = resolve_paths()
        auth = AuthService(paths)
        config = load_config(paths)
        monkeypatch.setattr(auth_module.sys, "stdin", FakeStdin())
        with pytest.raises(CommandError) as excinfo:
            auth._manual_login(config=config)
    finally:
        if old_home is None:
            os.environ.pop("KAIST_CLI_HOME", None)
        else:
            os.environ["KAIST_CLI_HOME"] = old_home

    assert excinfo.value.code == "AUTH_FLOW_UNSUPPORTED"
    assert "--username" in (excinfo.value.hint or "")


def test_install_browser_reports_linux_dependency_hint(tmp_path: Path, monkeypatch) -> None:
    old_home = os.environ.get("KAIST_CLI_HOME")
    os.environ["KAIST_CLI_HOME"] = str(tmp_path / "kaist-home")
    try:
        paths = resolve_paths()
        monkeypatch.setattr(auth_module.sys, "platform", "linux")
        monkeypatch.setattr(auth_browser_module, "configure_playwright_env", lambda paths: tmp_path / "pw-browsers")
        monkeypatch.setattr(auth_browser_module, "_playwright_install_cmd", lambda paths: (["playwright"], {}))
        monkeypatch.setattr(
            auth_browser_module.subprocess,
            "run",
            lambda *args, **kwargs: subprocess.CompletedProcess(
                args=args[0],
                returncode=1,
                stdout="",
                stderr="Host system is missing dependencies to run browsers.",
            ),
        )

        with pytest.raises(CommandError) as excinfo:
            auth_module.install_browser(paths)
    finally:
        if old_home is None:
            os.environ.pop("KAIST_CLI_HOME", None)
        else:
            os.environ["KAIST_CLI_HOME"] = old_home

    error = excinfo.value
    assert error.code == "BROWSER_INSTALL_FAILED"
    assert "linux browser/system dependencies" in (error.hint or "").lower()


def test_page_has_authenticated_session_false_when_content_unreadable() -> None:
    config = KlmsConfig(
        base_url="https://klms.kaist.ac.kr",
        dashboard_path="/my/",
        auth_username=None,
        auth_strategy="easy_login",
        otp_source=None,
        course_ids=(),
        notice_board_ids=(),
        exclude_course_title_patterns=(),
    )

    class BrokenPage:
        url = "https://klms.kaist.ac.kr/my/"

        def content(self) -> str:
            raise RuntimeError("page closed")

    assert auth_module._page_has_authenticated_klms_session(BrokenPage(), config=config) is False


def test_maybe_load_config_raises_on_invalid_config(tmp_path: Path) -> None:
    monkey_home = tmp_path / "kaist-home"
    config_path = monkey_home / "private" / "klms" / "config.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        'base_url = "https://klms.kaist.ac.kr"\nauth_strategy = "not-a-strategy"\n',
        encoding="utf-8",
    )
    old_home = os.environ.get("KAIST_CLI_HOME")
    os.environ["KAIST_CLI_HOME"] = str(monkey_home)
    try:
        paths = resolve_paths()
        with pytest.raises(CommandError) as exc_info:
            maybe_load_config(paths)
    finally:
        if old_home is None:
            os.environ.pop("KAIST_CLI_HOME", None)
        else:
            os.environ["KAIST_CLI_HOME"] = old_home
    assert exc_info.value.code == "CONFIG_INVALID"


def test_save_config_rewrites_invalid_existing_config(tmp_path: Path) -> None:
    monkey_home = tmp_path / "kaist-home"
    config_path = monkey_home / "private" / "klms" / "config.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text("this is not valid toml [[[", encoding="utf-8")
    old_home = os.environ.get("KAIST_CLI_HOME")
    os.environ["KAIST_CLI_HOME"] = str(monkey_home)
    try:
        paths = resolve_paths()
        saved = save_config(paths, base_url="https://klms.kaist.ac.kr", auth_username="student123")
        reloaded = load_config(paths)
    finally:
        if old_home is None:
            os.environ.pop("KAIST_CLI_HOME", None)
        else:
            os.environ["KAIST_CLI_HOME"] = old_home
    assert saved.base_url == "https://klms.kaist.ac.kr"
    assert reloaded.auth_username == "student123"
    assert reloaded.auth_strategy == "easy_login"


def test_live_check_reports_invalid_config_as_unknown(tmp_path: Path) -> None:
    monkey_home = tmp_path / "kaist-home"
    config_path = monkey_home / "private" / "klms" / "config.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        'base_url = "https://klms.kaist.ac.kr"\nauth_strategy = "bogus"\n',
        encoding="utf-8",
    )
    old_home = os.environ.get("KAIST_CLI_HOME")
    os.environ["KAIST_CLI_HOME"] = str(monkey_home)
    try:
        auth = AuthService(resolve_paths())
        result = auth.status(verify=True).data["live_check"]
    finally:
        if old_home is None:
            os.environ.pop("KAIST_CLI_HOME", None)
        else:
            os.environ["KAIST_CLI_HOME"] = old_home
    assert result["authenticated"] is None
    assert result["code"] == "CONFIG_INVALID"


def test_persist_worker_failure_swallows_persistence_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KAIST_CLI_HOME", str(tmp_path / "kaist-home"))
    _write_config(tmp_path)
    auth = AuthService(resolve_paths())

    def boom_update_auth_session(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise OSError("disk full")

    monkeypatch.setattr("kaist_cli.v2.klms.auth_session.update_auth_session", boom_update_auth_session)
    auth._persist_worker_failure(
        "sess-1",
        CommandError(code="AUTH_FAILED", message="otp rejected", exit_code=10),
    )


def test_request_get_surfaces_json_parse_failure(tmp_path: Path) -> None:
    class FakePage:
        url = "https://klms.kaist.ac.kr/lib/ajax/service.php"

        def evaluate(self, script: str, payload: dict[str, str]) -> dict[str, object]:
            return {
                "ok": True,
                "status": 200,
                "url": payload["url"],
                "contentType": "application/json",
                "text": "{not-json",
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

    assert result.data["json_parse_ok"] is False
    assert "parse_error" in result.data
    assert "body_json" not in result.data


def test_auth_status_surfaces_invalid_config_without_raising(tmp_path: Path) -> None:
    monkey_home = tmp_path / "kaist-home"
    config_path = monkey_home / "private" / "klms" / "config.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        'base_url = "https://klms.kaist.ac.kr"\nauth_strategy = "bogus"\n',
        encoding="utf-8",
    )
    old_home = os.environ.get("KAIST_CLI_HOME")
    os.environ["KAIST_CLI_HOME"] = str(monkey_home)
    try:
        auth = AuthService(resolve_paths())
        offline = auth.status(verify=False).data
        verified = auth.status(verify=True).data
    finally:
        if old_home is None:
            os.environ.pop("KAIST_CLI_HOME", None)
        else:
            os.environ["KAIST_CLI_HOME"] = old_home

    assert offline["configured"] is False
    assert offline["config"] is None
    assert offline["config_error"]["code"] == "CONFIG_INVALID"
    assert "rewrite the invalid config" in offline["recommended_action"]

    live_check = verified["live_check"]
    assert live_check["authenticated"] is None
    assert live_check["code"] == "CONFIG_INVALID"
    assert verified["config_error"]["code"] == "CONFIG_INVALID"


def test_auth_refresh_rewrites_invalid_config_via_login(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkey_home = tmp_path / "kaist-home"
    config_path = monkey_home / "private" / "klms" / "config.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text("this is not valid toml [[[", encoding="utf-8")
    monkeypatch.setenv("KAIST_CLI_HOME", str(monkey_home))
    auth = AuthService(resolve_paths())

    def fake_login(**kwargs):  # type: ignore[no-untyped-def]
        saved = save_config(
            auth._paths,
            base_url=kwargs.get("base_url") or "https://klms.kaist.ac.kr",
            dashboard_path=kwargs.get("dashboard_path"),
            auth_username=kwargs.get("username") or "student123",
        )
        return CommandResult(
            data={"ok": True, "auth_username": saved.auth_username, "auth_strategy": saved.auth_strategy},
            source="bootstrap",
            capability="partial",
        )

    monkeypatch.setattr(auth, "login", fake_login)
    result = auth.refresh(base_url="https://klms.kaist.ac.kr", username="student123")
    reloaded = load_config(auth._paths)
    assert result.data["ok"] is True
    assert reloaded.auth_username == "student123"
    assert reloaded.auth_strategy == "easy_login"


def test_require_email_otp_config_raises_clearly_on_invalid_config(tmp_path: Path) -> None:
    monkey_home = tmp_path / "kaist-home"
    config_path = monkey_home / "private" / "klms" / "config.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    corrupt = 'base_url = "https://klms.kaist.ac.kr"\nauth_strategy = "bogus"\n'
    config_path.write_text(corrupt, encoding="utf-8")
    old_home = os.environ.get("KAIST_CLI_HOME")
    os.environ["KAIST_CLI_HOME"] = str(monkey_home)
    try:
        auth = AuthService(resolve_paths())
        with pytest.raises(CommandError) as exc_info:
            auth._require_email_otp_config(
                base_url="https://klms.kaist.ac.kr",
                username="student123",
            )
        assert config_path.read_text(encoding="utf-8") == corrupt
    finally:
        if old_home is None:
            os.environ.pop("KAIST_CLI_HOME", None)
        else:
            os.environ["KAIST_CLI_HOME"] = old_home

    assert exc_info.value.code == "CONFIG_INVALID"
    assert "setup-email-otp" in (exc_info.value.hint or "")
