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
    assert payload["error"]["code"] == "CONFIG_INVALID"


def test_human_error_format(tmp_path: Path) -> None:
    cp = run_cli(tmp_path, "klms", "config", "set", "--base-url", "foo")
    assert cp.returncode == 40
    assert "error [config_invalid]" in cp.stderr.lower()
