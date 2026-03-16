from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _run_main(*args: str) -> subprocess.CompletedProcess[str]:
    env = {"PYTHONPATH": str(ROOT / "src")}
    return subprocess.run(
        [sys.executable, "-m", "kaist_cli.main", *args],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_root_help_mentions_global_agent_usage() -> None:
    cp = _run_main("--help")
    assert cp.returncode == 0, cp.stderr
    assert "kaist --agent klms today" in cp.stdout
    assert "kaist --agent version" in cp.stdout


def test_sync_run_help_explains_refresh_behavior() -> None:
    cp = _run_main("klms", "sync", "run", "-h")
    assert cp.returncode == 0, cp.stderr
    assert "Refresh cached notice and file data" in cp.stdout


def test_today_and_inbox_help_explain_difference() -> None:
    today = _run_main("klms", "today", "-h")
    inbox = _run_main("klms", "inbox", "-h")
    assert today.returncode == 0, today.stderr
    assert inbox.returncode == 0, inbox.stderr
    assert "urgency-focused" in today.stdout
    assert "chronological" in inbox.stdout


def test_skill_mentions_agent_envelope_and_auth_setup() -> None:
    body = (ROOT / "skills" / "kaist-cli" / "SKILL.md").read_text(encoding="utf-8")
    for needle in (
        "`ok`",
        "`schema`",
        "`meta`",
        "`data`",
        "`kaist --agent ...`",
        "`kaist klms auth install-browser`",
        "`kaist klms auth login --base-url https://klms.kaist.ac.kr --username KAIST_ID`",
        "`--since`",
        "`courses show`",
        "`assignments show`",
        "`notices show`",
        "`videos show`",
        "`files get`",
    ):
        assert needle in body
