from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"

# Ensure src is importable for in-process tests that import kaist_cli directly.
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))


class _FakeSecretStore:
    """Minimal SecretStore stand-in for AuthService email-OTP tests."""

    def __init__(self) -> None:
        self.saved: tuple[str, str] | None = None
        self.deleted: str | None = None

    def store_email_otp_password(self, *, username: str, password: str) -> None:
        self.saved = (username, password)

    def load_email_otp_password(self, *, username: str) -> str:
        if self.saved and self.saved[0] == username:
            return self.saved[1]
        raise KeyError(username)

    def delete_email_otp_password(self, *, username: str) -> None:
        self.deleted = username


def run_cli(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    env["KAIST_CLI_HOME"] = str(tmp_path / "kaist-home")
    return subprocess.run(
        [sys.executable, "-m", "kaist_cli", *args],
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
    auth_strategy: str = "easy_login",
    otp_source: str | None = None,
) -> Path:
    config_path = tmp_path / "kaist-home" / "private" / "klms" / "config.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        "\n".join(
            [
                f'base_url = "{base_url}"',
                f'dashboard_path = "{dashboard_path}"',
                f'auth_username = "{auth_username or ""}"',
                f'auth_strategy = "{auth_strategy}"',
                f'otp_source = "{otp_source or ""}"',
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
