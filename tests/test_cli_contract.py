from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


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


def test_help_mentions_bundled_skill_path(tmp_path: Path) -> None:
    cp = run_cli(tmp_path, "--help")
    assert cp.returncode == 0
    assert str(ROOT / "skills" / "kaist-cli") in cp.stdout


def test_legacy_flat_commands_are_removed(tmp_path: Path) -> None:
    cp = run_cli(tmp_path, "klms", "list", "courses")
    assert cp.returncode != 0
    assert "invalid choice" in cp.stderr.lower()
