from __future__ import annotations

import json
from pathlib import Path

import pytest

from cli_helpers import _write_config, run_cli


@pytest.mark.parametrize(
    "args",
    [
        ("klms", "courses", "list"),
        ("klms", "assignments", "list"),
        ("klms", "assignments", "show", "1210516"),
        ("klms", "notices", "list"),
        ("klms", "notices", "show", "331333"),
        ("klms", "files", "list"),
        ("klms", "files", "get", "991"),
        ("klms", "files", "download", "991"),
        ("klms", "files", "pull", "--limit", "1"),
        ("klms", "videos", "list"),
        ("klms", "videos", "show", "1205162"),
        ("klms", "today"),
        ("klms", "inbox"),
        ("klms", "sync", "run"),
        ("klms", "dev", "discover"),
        ("klms", "dev", "discover", "--manual-courseboard-seconds", "5"),
    ],
)
def test_command_requires_auth_artifact(tmp_path: Path, args: tuple[str, ...]) -> None:
    _write_config(tmp_path)
    cp = run_cli(tmp_path, "--json", *args)
    assert cp.returncode == 10
    payload = json.loads(cp.stdout)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "AUTH_MISSING"
