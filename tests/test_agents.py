from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from kaist_cli.core import agents


ROOT = Path(__file__).resolve().parents[1]


def test_resolve_agent_install_spec_codex_uses_codex_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))

    spec = agents.resolve_agent_install_spec("codex")

    assert spec.root == tmp_path / "codex-home" / "skills"
    assert spec.target_path == tmp_path / "codex-home" / "skills" / "kaist-cli"


def test_install_agent_codex_creates_symlink(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))

    payload = agents.install_agent("codex")

    target = Path(payload["target_path"])
    assert payload["agent"] == "codex"
    assert payload["created"] is True
    assert payload["mode"] == "symlink"
    assert target.is_symlink()
    assert target.resolve() == ROOT / "skills" / "kaist-cli"


def test_install_agent_custom_appends_skill_name(tmp_path: Path) -> None:
    payload = agents.install_agent("custom", custom_path=str(tmp_path / "targets"))

    assert payload["agent"] == "custom"
    assert payload["target_path"] == str(tmp_path / "targets" / "kaist-cli")
    assert (tmp_path / "targets" / "kaist-cli").is_symlink()


def test_agent_status_reports_known_targets(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home_dir = tmp_path / "home"
    monkeypatch.setattr(agents.Path, "home", lambda: home_dir)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    agents.install_agent("claude")

    payload = agents.agent_status(custom_path=str(tmp_path / "custom-root"))

    by_agent = {item["agent"]: item for item in payload["agents"]}
    assert payload["bundled_skill_path"] == str(ROOT / "skills" / "kaist-cli")
    assert set(by_agent) == {"codex", "claude", "gemini", "custom"}
    assert by_agent["claude"]["installed"] is True
    assert by_agent["claude"]["mode"] == "symlink"
    assert by_agent["custom"]["installed"] is False


def test_uninstall_agent_removes_target(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    agents.install_agent("codex")

    payload = agents.uninstall_agent("codex")

    assert payload["removed"] is True
    assert payload["installed"] is False
    assert not Path(payload["target_path"]).exists()


def test_install_agent_requires_force_for_existing_target(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    target = tmp_path / "codex-home" / "skills" / "kaist-cli"
    target.mkdir(parents=True, exist_ok=True)

    with pytest.raises(agents.AgentCommandError, match="Target path already exists"):
        agents.install_agent("codex")


def test_agent_cli_status_schema(tmp_path: Path) -> None:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    cp = subprocess.run(
        [sys.executable, "-m", "kaist_cli.main", "--agent", "agent", "status"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert cp.returncode == 0, cp.stderr
    payload = json.loads(cp.stdout)
    assert payload["schema"] == "kaist.cli.agent.status.v1"
    assert payload["ok"] is True
    assert isinstance(payload["data"]["agents"], list)
