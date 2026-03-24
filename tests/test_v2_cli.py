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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kaist_cli.v2.klms import auth as auth_module
from kaist_cli.cli.output import emit_text
from kaist_cli.v2.contracts import CommandError, CommandResult
from kaist_cli.v2.klms.cache import load_cache_entry, load_cache_value, save_cache_value
from kaist_cli.v2.klms import dashboard as dashboard_module
from kaist_cli.v2.klms.auth import AuthService
from kaist_cli.v2.klms.auth import _EasyLoginSignals, _extract_easy_login_error_message, _extract_easy_login_number, _extract_sso_login_view_url, looks_login_url
from kaist_cli.v2.klms.assignments import AssignmentService, _extract_assignment_detail_from_html, _extract_assignment_rows_from_calendar_data, _filter_assignments
from kaist_cli.v2.klms.courses import _course_is_current_term, _course_matches_query, _discover_courses_from_dashboard, _parse_recent_courses_payload
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
from kaist_cli.v2.klms.session import KlmsDownloadFallback
from kaist_cli.v2.klms.videos import VideoService, _extract_video_items_from_html, _parse_video_detail_from_html, _parse_video_viewer_from_html


FIXTURES = ROOT / "tests" / "fixtures"


def run_v2(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    env["KAIST_CLI_HOME"] = str(tmp_path / "kaist-home")
    return subprocess.run(
        [sys.executable, "-m", "kaist_cli.v2", *args],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _write_config(
    tmp_path: Path,
    *,
    base_url: str = "https://klms.kaist.ac.kr",
    dashboard_path: str = "/my/",
    auth_username: str | None = None,
) -> Path:
    config_path = tmp_path / "kaist-home" / "private" / "klms" / "config.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        "\n".join(
            [
                f'base_url = "{base_url}"',
                f'dashboard_path = "{dashboard_path}"',
                f'auth_username = "{auth_username or ""}"',
                "course_ids = []",
                "notice_board_ids = []",
                "exclude_course_title_patterns = []",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return config_path


def _write_storage_state(tmp_path: Path) -> Path:
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
                        "expires": 2_100_000_000,
                    }
                ],
                "origins": [],
            }
        ),
        encoding="utf-8",
    )
    return storage_state_path


def _read_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_auth_status_json_envelope(tmp_path: Path) -> None:
    cp = run_v2(tmp_path, "--json", "klms", "auth", "status")
    assert cp.returncode == 0, cp.stderr
    payload = json.loads(cp.stdout)
    assert payload["ok"] is True
    assert payload["schema"] == "kaist.klms.auth.status.v1"
    assert payload["meta"]["capability"] == "partial"
    assert payload["data"]["auth_mode"] == "none"
    assert payload["data"]["configured"] is False


def test_auth_status_detects_storage_state_and_cookie_stats(tmp_path: Path) -> None:
    _write_config(tmp_path)
    _write_storage_state(tmp_path)

    cp = run_v2(tmp_path, "--json", "klms", "auth", "status")
    assert cp.returncode == 0, cp.stderr
    payload = json.loads(cp.stdout)
    assert payload["data"]["configured"] is True
    assert payload["data"]["auth_mode"] == "storage_state"
    assert payload["data"]["storage_state_cookie_stats"]["cookie_count"] == 1
    assert payload["data"]["storage_state_cookie_stats"]["next_expiry_iso"] is not None


def test_auth_status_includes_saved_auth_username(tmp_path: Path) -> None:
    _write_config(tmp_path, auth_username="student123")

    cp = run_v2(tmp_path, "--json", "klms", "auth", "status")
    assert cp.returncode == 0, cp.stderr
    payload = json.loads(cp.stdout)
    assert payload["data"]["config"]["auth_username"] == "student123"


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

    assert exc_info.value.code == "AUTH_EXPIRED"
    assert calls[:2] == ["configure", "sync_playwright"]


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
        cp = run_v2(tmp_path, "--json", "klms", "courses", "list")
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


def test_dev_plan_json_envelope(tmp_path: Path) -> None:
    cp = run_v2(tmp_path, "--json", "klms", "dev", "plan")
    assert cp.returncode == 0, cp.stderr
    payload = json.loads(cp.stdout)
    assert payload["ok"] is True
    assert payload["schema"] == "kaist.klms.dev.plan.v1"
    assert payload["data"]["branch"] == "codex/klms-v2"


def test_dev_probe_includes_custom_login_paths_and_provider_candidates(tmp_path: Path) -> None:
    _write_config(tmp_path)

    cp = run_v2(tmp_path, "--json", "klms", "dev", "probe")
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
    cp = run_v2(tmp_path, "--json", "klms", "dev", "probe", "--live")
    assert cp.returncode == 0, cp.stderr
    payload = json.loads(cp.stdout)
    assert payload["ok"] is True
    assert payload["data"]["validation_mode"] == "live"
    assert payload["data"]["live_validation"]["status"] == "skipped"


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

        monkeypatch.setattr(auth_module, "EASY_LOGIN_POLL_SECONDS", 0.0)
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
        monkeypatch.setattr(auth_module, "EASY_LOGIN_POLL_SECONDS", 0.0)
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

        monkeypatch.setattr(auth_module, "EASY_LOGIN_POLL_SECONDS", 0.0)
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
        monkeypatch.setattr(auth_module, "EASY_LOGIN_POLL_SECONDS", 0.0)
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
        monkeypatch.setattr(auth_module, "EASY_LOGIN_POLL_SECONDS", 0.0)
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

        monkeypatch.setattr(auth_module, "EASY_LOGIN_POLL_SECONDS", 0.0)
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
            auth_module,
            "_launch_chromium_persistent_context_sync",
            lambda *args, **kwargs: fake_context,  # noqa: ARG005
        )
        monkeypatch.setattr(
            auth_module,
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
        monkeypatch.setattr(auth_module, "configure_playwright_env", lambda paths: tmp_path / "pw-browsers")
        monkeypatch.setattr(auth_module, "_playwright_install_cmd", lambda paths: (["playwright"], {}))
        monkeypatch.setattr(
            auth_module.subprocess,
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


def test_courses_list_requires_auth_artifact(tmp_path: Path) -> None:
    _write_config(tmp_path)
    cp = run_v2(tmp_path, "--json", "klms", "courses", "list")
    assert cp.returncode == 10
    payload = json.loads(cp.stdout)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "AUTH_MISSING"


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


def test_assignments_list_requires_auth_artifact(tmp_path: Path) -> None:
    _write_config(tmp_path)
    cp = run_v2(tmp_path, "--json", "klms", "assignments", "list")
    assert cp.returncode == 10
    payload = json.loads(cp.stdout)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "AUTH_MISSING"


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


def test_assignments_show_requires_auth_artifact(tmp_path: Path) -> None:
    _write_config(tmp_path)
    cp = run_v2(tmp_path, "--json", "klms", "assignments", "show", "1210516")
    assert cp.returncode == 10
    payload = json.loads(cp.stdout)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "AUTH_MISSING"


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


def test_notices_list_requires_auth_artifact(tmp_path: Path) -> None:
    _write_config(tmp_path)
    cp = run_v2(tmp_path, "--json", "klms", "notices", "list")
    assert cp.returncode == 10
    payload = json.loads(cp.stdout)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "AUTH_MISSING"


def test_notices_show_requires_auth_artifact(tmp_path: Path) -> None:
    _write_config(tmp_path)
    cp = run_v2(tmp_path, "--json", "klms", "notices", "show", "331333")
    assert cp.returncode == 10
    payload = json.loads(cp.stdout)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "AUTH_MISSING"


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
            "kaist_cli.v2.klms.files._load_recent_courses_from_bootstrap",
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
            "kaist_cli.v2.klms.videos._load_recent_courses_from_bootstrap",
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


def test_files_list_requires_auth_artifact(tmp_path: Path) -> None:
    _write_config(tmp_path)
    cp = run_v2(tmp_path, "--json", "klms", "files", "list")
    assert cp.returncode == 10
    payload = json.loads(cp.stdout)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "AUTH_MISSING"


def test_files_get_requires_auth_artifact(tmp_path: Path) -> None:
    _write_config(tmp_path)
    cp = run_v2(tmp_path, "--json", "klms", "files", "get", "991")
    assert cp.returncode == 10
    payload = json.loads(cp.stdout)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "AUTH_MISSING"


def test_files_download_requires_auth_artifact(tmp_path: Path) -> None:
    _write_config(tmp_path)
    cp = run_v2(tmp_path, "--json", "klms", "files", "download", "991")
    assert cp.returncode == 10
    payload = json.loads(cp.stdout)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "AUTH_MISSING"


def test_files_pull_requires_auth_artifact(tmp_path: Path) -> None:
    _write_config(tmp_path)
    cp = run_v2(tmp_path, "--json", "klms", "files", "pull", "--limit", "1")
    assert cp.returncode == 10
    payload = json.loads(cp.stdout)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "AUTH_MISSING"


def test_videos_list_requires_auth_artifact(tmp_path: Path) -> None:
    _write_config(tmp_path)
    cp = run_v2(tmp_path, "--json", "klms", "videos", "list")
    assert cp.returncode == 10
    payload = json.loads(cp.stdout)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "AUTH_MISSING"


def test_videos_show_requires_auth_artifact(tmp_path: Path) -> None:
    _write_config(tmp_path)
    cp = run_v2(tmp_path, "--json", "klms", "videos", "show", "1205162")
    assert cp.returncode == 10
    payload = json.loads(cp.stdout)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "AUTH_MISSING"


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


def test_today_requires_auth_artifact(tmp_path: Path) -> None:
    _write_config(tmp_path)
    cp = run_v2(tmp_path, "--json", "klms", "today")
    assert cp.returncode == 10
    payload = json.loads(cp.stdout)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "AUTH_MISSING"


def test_inbox_requires_auth_artifact(tmp_path: Path) -> None:
    _write_config(tmp_path)
    cp = run_v2(tmp_path, "--json", "klms", "inbox")
    assert cp.returncode == 10
    payload = json.loads(cp.stdout)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "AUTH_MISSING"


def test_dev_discover_requires_auth_artifact(tmp_path: Path) -> None:
    _write_config(tmp_path)
    cp = run_v2(tmp_path, "--json", "klms", "dev", "discover")
    assert cp.returncode == 10
    payload = json.loads(cp.stdout)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "AUTH_MISSING"


def test_dev_discover_manual_courseboard_requires_auth_artifact(tmp_path: Path) -> None:
    _write_config(tmp_path)
    cp = run_v2(tmp_path, "--json", "klms", "dev", "discover", "--manual-courseboard-seconds", "5")
    assert cp.returncode == 10
    payload = json.loads(cp.stdout)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "AUTH_MISSING"


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

    cp = run_v2(tmp_path, "--json", "klms", "sync", "status")
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

    cp = run_v2(tmp_path, "--json", "klms", "sync", "reset")
    assert cp.returncode == 0, cp.stderr
    payload = json.loads(cp.stdout)
    assert payload["ok"] is True
    assert payload["data"]["removed_entries"] == 2
    assert payload["data"]["providers"]["notices"]["entry_count"] == 0
    assert payload["data"]["providers"]["files"]["entry_count"] == 0


def test_sync_run_requires_auth_artifact(tmp_path: Path) -> None:
    _write_config(tmp_path)
    cp = run_v2(tmp_path, "--json", "klms", "sync", "run")
    assert cp.returncode == 10
    payload = json.loads(cp.stdout)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "AUTH_MISSING"
