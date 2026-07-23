from __future__ import annotations

import os
import shutil
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ...core.state_store import file_lock
from ..contracts import CommandError
from .paths import KlmsPaths, configure_playwright_env

def _tail_text(text: str, *, max_lines: int = 20) -> str:
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return ""
    return "\n".join(lines[-max_lines:])

def _is_missing_browser_error(exc: BaseException) -> bool:
    message = str(exc).lower()
    return (
        "executable doesn't exist" in message
        or "download new browsers" in message
        or "playwright install" in message
    )

def _system_browser_channel_candidates() -> list[str]:
    override = os.environ.get("KAIST_KLMS_BROWSER_CHANNEL", "").strip()
    if override:
        return [override]
    return ["chrome", "msedge"]

def _system_chromium_executable_candidates() -> list[Path]:
    override = os.environ.get("KAIST_KLMS_BROWSER_EXECUTABLE", "").strip()
    if override:
        return [Path(override).expanduser()]

    candidates: list[Path] = []
    if sys.platform == "darwin":
        candidates.extend(
            [
                Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
                Path("/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"),
                Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
                Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
                Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                Path.home() / "Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
                Path.home() / "Applications/Chromium.app/Contents/MacOS/Chromium",
                Path.home() / "Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            ]
        )
    elif sys.platform.startswith("linux"):
        for command in (
            "google-chrome",
            "google-chrome-stable",
            "chromium",
            "chromium-browser",
            "brave-browser",
            "microsoft-edge",
            "msedge",
        ):
            resolved = shutil.which(command)
            if resolved:
                candidates.append(Path(resolved))

    seen: set[str] = set()
    unique: list[Path] = []
    for candidate in candidates:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    return unique

def _resolve_system_chromium_executable() -> str | None:
    for candidate in _system_chromium_executable_candidates():
        try:
            if candidate.exists() and candidate.is_file():
                return str(candidate)
        except OSError:
            continue
    return None

def _browser_override_launch_options() -> list[dict[str, Any]]:
    options: list[dict[str, Any]] = []
    for channel in _system_browser_channel_candidates():
        options.append({"channel": channel, "_label": f"channel={channel}"})
    executable_path = _resolve_system_chromium_executable()
    if executable_path:
        options.append({"executable_path": executable_path, "_label": f"executable_path={executable_path}"})
    return options

def _browser_fallback_error(prefix: str, errors: list[str]) -> RuntimeError:
    detail = _tail_text("\n".join(errors), max_lines=16) or "unknown error"
    return RuntimeError(f"{prefix}. Details:\n{detail}")

def _concurrent_profile_access_error(*, lock_path: Path) -> CommandError:
    return CommandError(
        code="CONCURRENT_ACCESS",
        message="Another `kaist klms` command is already using the shared KLMS browser profile.",
        hint=(
            "Wait for the other KLMS command to finish, then retry. "
            f"If needed, check the lock file at {lock_path}."
        ),
        exit_code=20,
        retryable=True,
    )

@contextmanager
def _hold_profile_lock(paths: KlmsPaths) -> Any:
    try:
        with file_lock(paths.profile_lock_path, blocking=False):
            yield
    except BlockingIOError as exc:
        raise _concurrent_profile_access_error(lock_path=paths.profile_lock_path) from exc

def _playwright_install_cmd(paths: KlmsPaths) -> tuple[list[str], dict[str, str]]:
    configure_playwright_env(paths)
    from playwright._impl._driver import compute_driver_executable, get_driver_env  # type: ignore[import-untyped]

    node_path, cli_path = compute_driver_executable()
    env = os.environ.copy()
    env.update(get_driver_env())
    env["PLAYWRIGHT_BROWSERS_PATH"] = os.environ["PLAYWRIGHT_BROWSERS_PATH"]
    return [node_path, cli_path], env

def install_browser(paths: KlmsPaths, *, force: bool = False) -> dict[str, Any]:
    browser_path = configure_playwright_env(paths)
    driver_cmd, env = _playwright_install_cmd(paths)
    command = [*driver_cmd, "install"]
    if force:
        command.append("--force")
    command.append("chromium")
    completed = subprocess.run(  # noqa: S603
        command,
        check=False,
        env=env,
        capture_output=True,
        text=True,
    )
    result = {
        "ok": completed.returncode == 0,
        "browser": "chromium",
        "forced": force,
        "install_dir": str(browser_path),
        "command": command,
    }
    stdout_tail = _tail_text(completed.stdout)
    stderr_tail = _tail_text(completed.stderr)
    if stdout_tail:
        result["stdout_tail"] = stdout_tail
    if stderr_tail:
        result["stderr_tail"] = stderr_tail
    if completed.returncode != 0:
        detail = stderr_tail or stdout_tail or f"exit code {completed.returncode}"
        hint = "Run the same command again after fixing browser/runtime issues."
        detail_lower = detail.lower()
        if sys.platform.startswith("linux") and (
            "host system is missing dependencies" in detail_lower
            or "missing libraries" in detail_lower
            or "install-deps" in detail_lower
        ):
            hint = (
                "Install the required Linux browser/system dependencies on a supported x86_64 glibc host, "
                "then rerun `kaist klms auth install-browser`."
            )
        raise CommandError(
            code="BROWSER_INSTALL_FAILED",
            message=f"Failed to install Playwright Chromium ({detail}).",
            hint=hint,
            exit_code=50,
        )
    return result

def _launch_chromium_persistent_context_sync(
    playwright: Any,
    *,
    paths: KlmsPaths,
    user_data_dir: str,
    headless: bool,
    accept_downloads: bool,
) -> Any:
    launch_kwargs = {
        "user_data_dir": user_data_dir,
        "headless": headless,
        "accept_downloads": accept_downloads,
    }
    try:
        return playwright.chromium.launch_persistent_context(**launch_kwargs)
    except Exception as exc:  # noqa: BLE001
        if not _is_missing_browser_error(exc):
            raise
        errors = [f"default bundled Chromium missing: {exc}"]

    for option in _browser_override_launch_options():
        kwargs = dict(launch_kwargs)
        label = str(option.get("_label") or "override")
        kwargs.update({key: value for key, value in option.items() if key != "_label"})
        try:
            return playwright.chromium.launch_persistent_context(**kwargs)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{label}: {exc}")

    try:
        install_browser(paths, force=False)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"playwright install chromium: {exc}")
        raise _browser_fallback_error("Failed to launch browser and automatic install also failed", errors) from exc

    try:
        return playwright.chromium.launch_persistent_context(**launch_kwargs)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"after install retry: {exc}")
        raise _browser_fallback_error("Failed to launch browser after installation retry", errors) from exc

def _launch_chromium_browser_sync(playwright: Any, *, paths: KlmsPaths, headless: bool) -> Any:
    launch_kwargs = {"headless": headless}
    try:
        return playwright.chromium.launch(**launch_kwargs)
    except Exception as exc:  # noqa: BLE001
        if not _is_missing_browser_error(exc):
            raise
        errors = [f"default bundled Chromium missing: {exc}"]

    for option in _browser_override_launch_options():
        kwargs = dict(launch_kwargs)
        label = str(option.get("_label") or "override")
        kwargs.update({key: value for key, value in option.items() if key != "_label"})
        try:
            return playwright.chromium.launch(**kwargs)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{label}: {exc}")

    try:
        install_browser(paths, force=False)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"playwright install chromium: {exc}")
        raise _browser_fallback_error("Failed to launch browser and automatic install also failed", errors) from exc

    try:
        return playwright.chromium.launch(**launch_kwargs)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"after install retry: {exc}")
        raise _browser_fallback_error("Failed to launch browser after installation retry", errors) from exc
