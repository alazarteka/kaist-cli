from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from contextlib import redirect_stdout
from datetime import datetime
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
from kaist_cli.v2.klms.assignments import _extract_assignment_detail_from_html, _extract_assignment_rows_from_calendar_data, _filter_assignments
from kaist_cli.v2.klms.courses import _course_is_current_term, _discover_courses_from_dashboard, _parse_recent_courses_payload
from kaist_cli.v2.klms.capture import _courseboard_runtime_capture_summary, _extract_courseboard_js_hints
from kaist_cli.v2.klms.config import load_config
from kaist_cli.v2.klms.dashboard import DashboardService, _build_inbox_items, _decorate_today_assignments, _filter_inbox_assignments, _select_recent_notices
from kaist_cli.v2.klms.discovery import load_recent_courses_args, map_discovery_report
from kaist_cli.v2.klms.files import FileService, _extract_file_items_from_course_contents, _extract_file_items_from_html, _pull_subdir_for_item, _sanitize_relpath, _synthesize_file_item_from_url, _unwrap_moodle_ajax_payload
from kaist_cli.v2.klms.models import Assignment, Course, FileItem
from kaist_cli.v2.klms.notices import NoticeService, _discover_notice_board_ids_from_course_page, _extract_course_ids_from_dashboard, _parse_notice_detail_from_html, _parse_notice_items_from_soup
from kaist_cli.v2.klms.paths import resolve_paths
from kaist_cli.v2.klms.provider_state import ProviderLoad
from kaist_cli.v2.klms.videos import _extract_video_items_from_html, _parse_video_detail_from_html, _parse_video_viewer_from_html


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
    assert assignments[0].course_code == "MAS101-(AP)"
    assert assignments[0].due_raw == "Friday, 24 March, 10:25"
    assert assignments[0].due_iso == "2023-03-24T01:25:00Z"


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
      <div class="article-content"><p>시험 범위를 확인하세요.</p><a href="/pluginfile.php/123/file.pdf">file.pdf</a></div>
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
    assert notice.attachments[0]["filename"] == "file.pdf"


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
    assert _pull_subdir_for_item(item, base_subdir="spring26") == "spring26/CS.30000_2026_1__Introduction_to_Algorithms"


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
