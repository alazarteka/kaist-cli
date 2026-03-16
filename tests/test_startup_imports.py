from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _run_python(script: str) -> subprocess.CompletedProcess[str]:
    env = {"PYTHONPATH": str(ROOT / "src")}
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_build_parser_does_not_import_live_klms_modules() -> None:
    script = """
import json
import sys
TARGETS = [
    "kaist_cli.v2.klms.container",
    "kaist_cli.v2.klms.auth",
    "kaist_cli.v2.klms.assignments",
    "kaist_cli.v2.klms.notices",
    "kaist_cli.v2.klms.files",
    "kaist_cli.v2.klms.videos",
]
from kaist_cli.cli.parser import build_parser
build_parser()
print(json.dumps({name: (name in sys.modules) for name in TARGETS}))
"""
    cp = _run_python(script)
    assert cp.returncode == 0, cp.stderr
    payload = json.loads(cp.stdout)
    assert payload == {
        "kaist_cli.v2.klms.container": False,
        "kaist_cli.v2.klms.auth": False,
        "kaist_cli.v2.klms.assignments": False,
        "kaist_cli.v2.klms.notices": False,
        "kaist_cli.v2.klms.files": False,
        "kaist_cli.v2.klms.videos": False,
    }


def test_version_command_does_not_import_live_klms_modules() -> None:
    script = """
import contextlib
import io
import json
import sys
TARGETS = [
    "kaist_cli.v2.klms.container",
    "kaist_cli.v2.klms.auth",
]
from kaist_cli.main import main
stdout = io.StringIO()
stderr = io.StringIO()
with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
    rc = main(["version"])
print(json.dumps({"rc": rc, "targets": {name: (name in sys.modules) for name in TARGETS}}))
"""
    cp = _run_python(script)
    assert cp.returncode == 0, cp.stderr
    payload = json.loads(cp.stdout)
    assert payload["rc"] == 0
    assert payload["targets"] == {
        "kaist_cli.v2.klms.container": False,
        "kaist_cli.v2.klms.auth": False,
    }
