from __future__ import annotations

import json
import os
import subprocess
import sys
import fcntl
from pathlib import Path

import pytest

from kaist_cli.cli.output import emit_text


ROOT = Path(__file__).resolve().parents[1]


def run_cli(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["KAIST_CLI_HOME"] = str(tmp_path / "kaist-home")
    env["PYTHONPATH"] = str(ROOT / "src")
    return subprocess.run(
        [sys.executable, "-m", "kaist_cli.main", *args],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_agent_success_envelope_for_auth_status(tmp_path: Path) -> None:
    cp = run_cli(tmp_path, "--agent", "klms", "auth", "status")
    assert cp.returncode == 0, cp.stderr
    payload = json.loads(cp.stdout)
    assert payload["ok"] is True
    assert payload["schema"] == "kaist.klms.auth.status.v1"
    assert isinstance(payload.get("generated_at"), str)
    assert payload["meta"]["source"] == "bootstrap"
    assert payload["meta"]["capability"] == "partial"
    assert "data" in payload


def test_agent_error_envelope_and_exit_code(tmp_path: Path) -> None:
    cp = run_cli(tmp_path, "--agent", "klms", "courses", "list")
    assert cp.returncode == 40
    payload = json.loads(cp.stdout)
    assert payload["ok"] is False
    assert payload["schema"] == "kaist.klms.courses.list.v1"
    assert payload["error"]["code"] == "CONFIG_MISSING"


def test_human_error_format(tmp_path: Path) -> None:
    cp = run_cli(tmp_path, "klms", "courses", "list")
    assert cp.returncode == 40
    assert "error [config_missing]" in cp.stderr.lower()


def test_new_command_schema_for_list_courses_error(tmp_path: Path) -> None:
    cp = run_cli(tmp_path, "--agent", "klms", "courses", "list")
    assert cp.returncode == 40
    payload = json.loads(cp.stdout)
    assert payload["schema"] == "kaist.klms.courses.list.v1"
    assert payload["ok"] is False


def test_files_pull_schema_is_stable(tmp_path: Path) -> None:
    cp = run_cli(tmp_path, "--agent", "klms", "files", "pull")
    assert cp.returncode == 40
    payload = json.loads(cp.stdout)
    assert payload["schema"] == "kaist.klms.files.pull.v1"
    assert payload["ok"] is False


def test_notices_list_accepts_course_id_flag(tmp_path: Path) -> None:
    cp = run_cli(tmp_path, "--agent", "klms", "notices", "list", "--course-id", "178223")
    assert cp.returncode == 40
    payload = json.loads(cp.stdout)
    assert payload["schema"] == "kaist.klms.notices.list.v1"
    assert payload["ok"] is False


def test_files_pull_accepts_course_query_flag(tmp_path: Path) -> None:
    cp = run_cli(tmp_path, "--agent", "klms", "files", "pull", "--course", "CS.30000")
    assert cp.returncode == 40
    payload = json.loads(cp.stdout)
    assert payload["schema"] == "kaist.klms.files.pull.v1"
    assert payload["ok"] is False


def test_notice_attachments_pull_schema_is_stable(tmp_path: Path) -> None:
    cp = run_cli(tmp_path, "--agent", "klms", "notices", "attachments", "pull", "--course-id", "178223")
    assert cp.returncode == 40
    payload = json.loads(cp.stdout)
    assert payload["schema"] == "kaist.klms.notices.attachments.pull.v1"
    assert payload["ok"] is False


def test_files_pull_dest_and_subdir_conflict_returns_structured_error(tmp_path: Path) -> None:
    cp = run_cli(tmp_path, "--agent", "klms", "files", "pull", "--dest", "/tmp/out", "--subdir", "managed")
    assert cp.returncode == 40
    payload = json.loads(cp.stdout)
    assert payload["schema"] == "kaist.klms.files.pull.v1"
    assert payload["ok"] is False
    assert payload["error"]["code"] == "CONFIG_INVALID"


def test_notice_attachments_pull_dest_and_subdir_conflict_returns_structured_error(tmp_path: Path) -> None:
    cp = run_cli(
        tmp_path,
        "--agent",
        "klms",
        "notices",
        "attachments",
        "pull",
        "--course-id",
        "178223",
        "--dest",
        "/tmp/out",
        "--subdir",
        "managed",
    )
    assert cp.returncode == 40
    payload = json.loads(cp.stdout)
    assert payload["schema"] == "kaist.klms.notices.attachments.pull.v1"
    assert payload["ok"] is False
    assert payload["error"]["code"] == "CONFIG_INVALID"


def test_agent_reports_concurrent_access_when_profile_lock_is_held(tmp_path: Path) -> None:
    private_root = tmp_path / "kaist-home" / "private" / "klms"
    private_root.mkdir(parents=True, exist_ok=True)
    (private_root / "config.toml").write_text(
        'base_url = "https://klms.kaist.ac.kr"\n'
        'dashboard_path = "/my/"\n'
        'auth_username = ""\n'
        "course_ids = []\n"
        "notice_board_ids = []\n"
        "exclude_course_title_patterns = []\n",
        encoding="utf-8",
    )
    profile_dir = private_root / "profile"
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "session-cookie").write_text("present", encoding="utf-8")

    lock_handle = open(private_root / ".lock", "a+b")
    try:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        cp = run_cli(tmp_path, "--agent", "klms", "courses", "list")
    finally:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        lock_handle.close()

    assert cp.returncode == 20
    payload = json.loads(cp.stdout)
    assert payload["ok"] is False
    assert payload["schema"] == "kaist.klms.courses.list.v1"
    assert payload["error"]["code"] == "CONCURRENT_ACCESS"
    assert payload["error"]["retryable"] is True


def test_sync_status_works_without_klms_config(tmp_path: Path) -> None:
    cp = run_cli(tmp_path, "--agent", "klms", "sync", "status")
    assert cp.returncode == 0
    payload = json.loads(cp.stdout)
    assert payload["ok"] is True
    assert payload["schema"] == "kaist.klms.sync.status.v1"
    assert payload["data"]["providers"]["notice_board_ids"]["entry_count"] == 0
    assert payload["data"]["providers"]["notices"]["entry_count"] == 0
    assert payload["data"]["providers"]["files"]["entry_count"] == 0



def test_version_command_schema(tmp_path: Path) -> None:
    cp = run_cli(tmp_path, "--agent", "version")
    assert cp.returncode == 0
    payload = json.loads(cp.stdout)
    assert payload["ok"] is True
    assert payload["schema"] == "kaist.cli.version.v1"
    assert isinstance(payload["data"]["version"], str)
    assert payload["data"]["distribution"] == "source"
    assert payload["data"]["install_root"] == str(ROOT)
    assert payload["data"]["bundled_skill_path"] == str(ROOT / "skills" / "kaist-cli")
    assert payload["data"]["self_update_supported"] is False
    assert payload["data"]["release_repo"] == "alazarteka/kaist-cli"


def test_version_supports_global_json_flag_after_command(tmp_path: Path) -> None:
    cp = run_cli(tmp_path, "version", "--json")
    assert cp.returncode == 0, cp.stderr
    payload = json.loads(cp.stdout)
    assert payload["ok"] is True
    assert payload["schema"] == "kaist.cli.version.v1"


def test_version_flag_is_supported(tmp_path: Path) -> None:
    cp = run_cli(tmp_path, "--version")
    assert cp.returncode == 0
    assert cp.stdout.strip() == "kaist 0.5.3"


def test_agent_parser_failures_are_one_json_envelope(tmp_path: Path) -> None:
    for args in (
        ("--agent", "klms", "files", "get"),
        ("--agent", "klms", "today", "--limit", "0"),
    ):
        cp = run_cli(tmp_path, *args)
        assert cp.returncode == 2
        payload = json.loads(cp.stdout)
        assert payload["ok"] is False
        assert payload["schema"] == "kaist.cli.parse.v1"
        assert payload["error"]["code"] == "CONFIG_INVALID"
        assert cp.stderr == ""


def test_complete_refresh_no_input_fails_without_prompt(tmp_path: Path) -> None:
    cp = run_cli(tmp_path, "--agent", "klms", "auth", "complete-refresh", "session-id", "--no-input")
    assert cp.returncode == 40
    payload = json.loads(cp.stdout)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "CONFIG_INVALID"
    assert "OTP" in payload["error"]["message"]


def test_complete_refresh_accepts_stdin_channel_without_echoing_code(tmp_path: Path) -> None:
    env = os.environ.copy()
    env["KAIST_CLI_HOME"] = str(tmp_path / "kaist-home")
    env["PYTHONPATH"] = str(ROOT / "src")
    cp = subprocess.run(
        [sys.executable, "-m", "kaist_cli.main", "--agent", "klms", "auth", "complete-refresh", "session-id", "--otp-stdin"],
        cwd=ROOT,
        env=env,
        input="123456\n",
        text=True,
        capture_output=True,
        check=False,
    )
    assert cp.returncode == 10
    payload = json.loads(cp.stdout)
    assert payload["ok"] is False
    assert "123456" not in cp.stdout


def test_human_rendering_sanitizes_terminal_controls_but_keeps_unicode(capsys: pytest.CaptureFixture[str]) -> None:
    emit_text(
        {
            "title": "강의\x1b]52;c;secret\x07\rspoof\nnext\u202e.txt",
            "warning": "안전한 안내",
        }
    )
    output = capsys.readouterr().out
    assert "강의" in output
    assert "안전한 안내" in output
    assert "secret" not in output
    assert "spoof" in output
    assert "\x1b" not in output
    assert "\r" not in output
    assert "\nnext" not in output


def test_help_mentions_bundled_skill_path(tmp_path: Path) -> None:
    cp = run_cli(tmp_path, "--help")
    assert cp.returncode == 0
    assert str(ROOT / "skills" / "kaist-cli") in cp.stdout


def test_legacy_flat_commands_are_removed(tmp_path: Path) -> None:
    cp = run_cli(tmp_path, "klms", "list", "courses")
    assert cp.returncode != 0
    assert "invalid choice" in cp.stderr.lower()
