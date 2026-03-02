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


def test_agent_success_envelope_for_config_set(tmp_path: Path) -> None:
    cp = run_cli(tmp_path, "--agent", "klms", "config", "set", "--base-url", "https://example.com")
    assert cp.returncode == 0, cp.stderr
    payload = json.loads(cp.stdout)
    assert payload["ok"] is True
    assert payload["schema"] == "kaist.klms.config.set.v1"
    assert isinstance(payload.get("generated_at"), str)
    assert "data" in payload


def test_agent_error_envelope_and_exit_code(tmp_path: Path) -> None:
    cp = run_cli(tmp_path, "--agent", "klms", "config", "set", "--base-url", "foo")
    assert cp.returncode == 40
    payload = json.loads(cp.stdout)
    assert payload["ok"] is False
    assert payload["schema"] == "kaist.klms.config.set.v1"
    assert payload["error"]["code"] == "CONFIG_INVALID"


def test_human_error_format(tmp_path: Path) -> None:
    cp = run_cli(tmp_path, "klms", "config", "set", "--base-url", "foo")
    assert cp.returncode == 40
    assert "error [config_invalid]" in cp.stderr.lower()


def test_new_command_schema_for_list_courses_error(tmp_path: Path) -> None:
    cp = run_cli(tmp_path, "--agent", "klms", "list", "courses", "--no-enrich")
    assert cp.returncode == 10
    payload = json.loads(cp.stdout)
    assert payload["schema"] == "kaist.klms.courses.v1"
    assert payload["ok"] is False


def test_get_file_schema_is_stable(tmp_path: Path) -> None:
    cp = run_cli(tmp_path, "--agent", "klms", "get", "file", "https://example.com/file.pdf")
    assert cp.returncode == 10
    payload = json.loads(cp.stdout)
    assert payload["schema"] == "kaist.klms.download.v1"
    assert payload["ok"] is False


def test_sync_status_works_without_klms_config(tmp_path: Path) -> None:
    cp = run_cli(tmp_path, "--agent", "klms", "sync", "status")
    assert cp.returncode == 0
    payload = json.loads(cp.stdout)
    assert payload["ok"] is True
    assert payload["schema"] == "kaist.klms.sync.status.v1"
    assert payload["data"]["snapshot_exists"] is False


def test_portal_scaffold_command(tmp_path: Path) -> None:
    cp = run_cli(tmp_path, "--agent", "portal", "status")
    assert cp.returncode == 0
    payload = json.loads(cp.stdout)
    assert payload["ok"] is True
    assert payload["schema"] == "kaist.portal.status.v1"
    assert payload["data"]["implemented"] is False


def test_version_command_schema(tmp_path: Path) -> None:
    cp = run_cli(tmp_path, "--agent", "version")
    assert cp.returncode == 0
    payload = json.loads(cp.stdout)
    assert payload["ok"] is True
    assert payload["schema"] == "kaist.cli.version.v1"
    assert isinstance(payload["data"]["version"], str)


def test_legacy_flat_commands_are_removed(tmp_path: Path) -> None:
    cp = run_cli(tmp_path, "klms", "courses")
    assert cp.returncode != 0
    assert "invalid choice" in cp.stderr.lower()
