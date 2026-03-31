from __future__ import annotations

import html as _html
import json
import os
import re
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urljoin

from bs4 import BeautifulSoup  # type: ignore[import-untyped]

from ...core.state_store import file_lock
from ..contracts import CommandError, CommandResult
from .config import KlmsConfig, maybe_load_config, save_config
from .paths import KlmsPaths, chmod_best_effort, configure_playwright_env, ensure_private_dirs


AuthMode = Literal["profile", "storage_state", "none"]

HTML_LOGIN_MARKERS = (
    "notloggedin",
    "page-login",
    "ssologin",
    "/local/applogin/",
    "result_login_json.php",
)

URL_LOGIN_NEEDLES = (
    "/login/",
    "ssologin",
    "oidc",
    "sso",
    "/local/applogin/",
    "result_login_json.php",
)

APP_LOGIN_PATHS = (
    "/local/applogin/result_login_json.php",
    "/login/ssologin.php",
)

EASY_LOGIN_VIEW_NEEDLE = "/auth/kaist/user/login/view"
EASY_LOGIN_SUBMIT_NEEDLE = "/auth/twofactor/mfa/login2Factor"
EASY_LOGIN_POLL_SECONDS = 1.0
EASY_LOGIN_DEFAULT_WAIT_SECONDS = 180.0


def epoch_to_iso_utc(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def has_profile_session(paths: KlmsPaths) -> bool:
    if not paths.profile_dir.exists() or not paths.profile_dir.is_dir():
        return False
    try:
        return any(paths.profile_dir.iterdir())
    except OSError:
        return False


def has_storage_state_session(paths: KlmsPaths) -> bool:
    return paths.storage_state_path.exists()


def active_auth_mode(paths: KlmsPaths) -> AuthMode:
    if has_profile_session(paths):
        return "profile"
    if has_storage_state_session(paths):
        return "storage_state"
    return "none"


def looks_logged_out_html(html: str) -> bool:
    text = html.lower()
    return any(marker in text for marker in HTML_LOGIN_MARKERS)


def looks_login_url(url: str) -> bool:
    lowered = (url or "").lower()
    return any(needle in lowered for needle in URL_LOGIN_NEEDLES)


def extract_sesskey(html: str) -> str | None:
    patterns = [
        r'"sesskey"\s*:\s*"([^"]+)"',
        r"sesskey=([A-Za-z0-9]+)",
        r'name=["\']sesskey["\']\s+value=["\']([^"\']+)["\']',
    ]
    for pattern in patterns:
        match = re.search(pattern, html)
        if match:
            value = match.group(1).strip()
            if value:
                return value
    return None


def _extract_sso_login_view_url(current_url: str, html: str) -> str | None:
    href_patterns = (
        r'href=["\']([^"\']*sso\.kaist\.ac\.kr/auth/kaist/user/login/view[^"\']*)["\']',
        r'href=["\']([^"\']*/auth/kaist/user/login/view[^"\']*)["\']',
    )
    for pattern in href_patterns:
        match = re.search(pattern, html, flags=re.IGNORECASE)
        if match:
            href = _html.unescape(match.group(1)).strip()
            if href:
                return urljoin(current_url, href)
    return None


def _extract_easy_login_error_message(html: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    for selector in ("#mfaResultMsg", "#resultMsg"):
        node = soup.select_one(selector)
        if node is None:
            continue
        text = " ".join(node.get_text(" ", strip=True).split())
        if text:
            return text
    return None


def _extract_easy_login_number(html: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    for selector in (
        ".auth_number .nember_wrap",
        ".auth_number [aria-hidden='false']",
        ".auth_number .sr-only",
        "#authNumber",
    ):
        node = soup.select_one(selector)
        if node is None:
            continue
        digits = re.sub(r"\D+", "", node.get_text("", strip=True))
        if 2 <= len(digits) <= 8:
            return digits

    text = " ".join(soup.get_text("\n", strip=True).split())
    patterns = (
        r"(?:verification\s*code|login\s*number|easy login\s*number|authentication\s*number|approval\s*number)\s*[:：]?\s*([0-9]{2,6})",
        r"(?:인증\s*코드|로그인\s*번호|간편\s*로그인\s*번호|인증\s*번호|승인\s*번호)\s*[:：]?\s*([0-9]{2,6})",
        r"(?:^|[\s(])number\s*[:：]?\s*([0-9]{4,6})(?:$|[\s)])",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return match.group(1)
    return None


def _should_update_easy_login_number(*, previous: str | None, current: str | None) -> bool:
    if not current or current == previous:
        return False
    if previous is None:
        return True
    if len(current) != len(previous):
        return False
    return True


def _looks_like_easy_login_page(url: str) -> bool:
    lowered = (url or "").lower()
    return EASY_LOGIN_VIEW_NEEDLE.lower() in lowered or EASY_LOGIN_SUBMIT_NEEDLE.lower() in lowered


def _safe_page_content(page: Any) -> str | None:
    try:
        return str(page.content() or "")
    except Exception:
        return None


def _safe_page_url(page: Any) -> str | None:
    try:
        url = str(page.url or "").strip()
    except Exception:
        return None
    return url or None


def _looks_like_easy_login_verification_page(html: str) -> bool:
    lowered = (html or "").lower()
    return any(
        needle in lowered
        for needle in (
            "auth_number",
            "nember_wrap",
            'id="countdown"',
            "waiting for verification",
            "verification code",
            "btn_code",
        )
    )


@dataclass
class _EasyLoginSignals:
    latest_mfa_payload: dict[str, Any] | None = None
    latest_policy_payload: dict[str, Any] | None = None


def _response_json_payload(response: Any) -> dict[str, Any] | None:
    try:
        text = str(response.text() or "")
    except Exception:
        return None
    if not text:
        return None
    try:
        payload = json.loads(text)
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _record_easy_login_signal(signals: _EasyLoginSignals, *, url: str, payload: dict[str, Any]) -> None:
    lowered = (url or "").lower()
    if "/auth/twofactor/mfa/auth" in lowered:
        signals.latest_mfa_payload = payload
    elif "/auth/kaist/user/login/check/policy" in lowered:
        signals.latest_policy_payload = payload


def _observe_easy_login_response(signals: _EasyLoginSignals, response: Any) -> None:
    url = str(getattr(response, "url", "") or "")
    lowered = url.lower()
    if "/auth/twofactor/mfa/auth" not in lowered and "/auth/kaist/user/login/check/policy" not in lowered:
        return
    payload = _response_json_payload(response)
    if payload is None:
        return
    _record_easy_login_signal(signals, url=url, payload=payload)


def _evaluate_easy_login_mfa_payload(payload: dict[str, Any]) -> str:
    if bool(payload.get("result")):
        return "approved"

    error_code = str(payload.get("error_code") or "").strip()
    if not error_code or error_code == "ESY020":
        return "waiting"
    if error_code == "E004":
        raise CommandError(
            code="AUTH_TIMEOUT",
            message="KAIST Easy Login approval timed out.",
            hint="Retry the request in the KAIST auth app, or use `kaist klms auth login` without `--username`.",
            exit_code=10,
            retryable=True,
        )

    messages = {
        "ESY021": "KAIST Easy Login is temporarily blocked because too many approvals failed.",
        "ESY022": "KAIST Easy Login is blocked because too many approvals failed.",
        "ESY023": "KAIST Easy Login approval was canceled in the KAIST auth app.",
        "ESY024": "KAIST Easy Login verification number did not match in the KAIST auth app.",
    }
    message = messages.get(error_code, f"KAIST Easy Login failed ({error_code}).")
    raise CommandError(
        code="AUTH_FAILED",
        message=message,
        hint="Try again, or fall back to `kaist klms auth login` without `--username`.",
        exit_code=10,
        retryable=False,
    )


def _evaluate_easy_login_policy_payload(payload: dict[str, Any]) -> str:
    code = str(payload.get("code") or "").strip()
    if not code:
        return "pending"
    if code == "SS0001":
        return "success"

    if code == "SS0099":
        return "device_registration"
    if code == "SS0007":
        raise CommandError(
            code="AUTH_FLOW_UNSUPPORTED",
            message="KAIST SSO reported an existing-session conflict that needs manual resolution.",
            hint="Use the manual browser login flow once to resolve the duplicate-session prompt.",
            exit_code=10,
            retryable=False,
        )
    if code in {"SS0004", "SS0005", "SS0006"}:
        raise CommandError(
            code="AUTH_FLOW_UNSUPPORTED",
            message="KAIST SSO requires a password change before KLMS login can complete.",
            hint="Use the manual browser login flow and complete the password-change step.",
            exit_code=10,
            retryable=False,
        )
    if code == "dormancy":
        raise CommandError(
            code="AUTH_FLOW_UNSUPPORTED",
            message="KAIST SSO requires dormant-account reactivation before KLMS login can complete.",
            hint="Reactivate the KAIST account in the browser, then retry Easy Login.",
            exit_code=10,
            retryable=False,
        )
    if code in {"ES0017", "EAU016", "EAU017", "EAU018"}:
        raise CommandError(
            code="AUTH_FAILED",
            message=f"KAIST SSO rejected the Easy Login request ({code}).",
            hint="Retry the login flow; if it persists, use manual browser login once.",
            exit_code=10,
            retryable=False,
        )

    raise CommandError(
        code="AUTH_FAILED",
        message=f"KAIST SSO policy check failed ({code}).",
        hint="Retry the login flow, or use manual browser login if the account needs extra steps.",
        exit_code=10,
        retryable=False,
    )


def _submit_easy_login_link(page: Any) -> bool:
    try:
        return bool(
            page.evaluate(
                """
                () => {
                  const form = document.querySelector('form[name="loginForm"]');
                  if (!form) return false;
                  form.action = "/auth/user/login/link";
                  form.submit();
                  return true;
                }
                """
            )
        )
    except Exception:
        return False


def _complete_easy_login_device_registration(page: Any) -> bool:
    try:
        submitted = bool(
            page.evaluate(
                """
                () => {
                  if (typeof setDevice === "function") {
                    setDevice();
                    return true;
                  }
                  const link = document.querySelector('a[href="javascript:setDevice();"]');
                  if (!link) return false;
                  link.click();
                  return true;
                }
                """
            )
        )
        if submitted:
            return True
    except Exception:
        pass
    return False


def _page_has_authenticated_klms_session(page: Any, *, config: KlmsConfig) -> bool:
    url = _safe_page_url(page)
    if not url:
        return False
    base_url = config.base_url.rstrip("/")
    if not url.startswith(base_url):
        return False
    if looks_login_url(url):
        return False
    html = _safe_page_content(page)
    if html is None:
        return True
    return not looks_logged_out_html(html)


def storage_state_cookie_stats(paths: KlmsPaths) -> dict[str, Any] | None:
    if not paths.storage_state_path.exists():
        return None
    try:
        raw = json.loads(paths.storage_state_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {"read_error": str(exc)}

    cookies = raw.get("cookies") or []
    now_epoch = time.time()
    exp_epochs = [
        float(cookie.get("expires"))
        for cookie in cookies
        if isinstance(cookie, dict)
        and isinstance(cookie.get("expires"), (int, float))
        and float(cookie.get("expires")) > 0
    ]
    if not exp_epochs:
        return {
            "cookie_count": len(cookies),
            "expiring_cookie_count": 0,
            "next_expiry_iso": None,
            "next_expiry_in_hours": None,
            "latest_expiry_iso": None,
        }

    next_expiry = min(exp_epochs)
    latest_expiry = max(exp_epochs)
    return {
        "cookie_count": len(cookies),
        "expiring_cookie_count": len(exp_epochs),
        "next_expiry_iso": epoch_to_iso_utc(next_expiry),
        "next_expiry_in_hours": round((next_expiry - now_epoch) / 3600, 2),
        "latest_expiry_iso": epoch_to_iso_utc(latest_expiry),
    }


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


class AuthService:
    def __init__(self, paths: KlmsPaths) -> None:
        self._paths = paths

    def _config_payload(self, config: KlmsConfig | None) -> dict[str, Any] | None:
        if config is None:
            return None
        return {
            "base_url": config.base_url,
            "dashboard_path": config.dashboard_path,
            "auth_username": config.auth_username,
            "course_ids": list(config.course_ids),
            "notice_board_ids": list(config.notice_board_ids),
            "exclude_course_title_patterns": list(config.exclude_course_title_patterns),
        }

    def _recommended_action(self, *, config: KlmsConfig | None, mode: AuthMode) -> str:
        if config is None:
            return "Run `kaist klms auth login --base-url https://klms.kaist.ac.kr`."
        if mode == "none":
            return "Run `kaist klms auth login` to create a persistent browser profile."
        return "Run `kaist klms auth refresh` if the saved session stops working."

    def snapshot(self) -> dict[str, Any]:
        ensure_private_dirs(self._paths)
        config = maybe_load_config(self._paths)
        mode = active_auth_mode(self._paths)
        return {
            "configured": config is not None,
            "config_path": str(self._paths.config_path),
            "profile_path": str(self._paths.profile_dir),
            "storage_state_path": str(self._paths.storage_state_path),
            "auth_mode": mode,
            "session_artifacts": {
                "profile": has_profile_session(self._paths),
                "storage_state": has_storage_state_session(self._paths),
            },
            "storage_state_cookie_stats": storage_state_cookie_stats(self._paths),
            "config": self._config_payload(config),
            "validation_mode": "offline-only",
            "login_detection": {
                "html_markers": list(HTML_LOGIN_MARKERS),
                "url_needles": list(URL_LOGIN_NEEDLES),
                "app_login_paths": list(APP_LOGIN_PATHS),
            },
            "recommended_action": self._recommended_action(config=config, mode=mode),
        }

    def status(self) -> CommandResult:
        return CommandResult(data=self.snapshot(), source="bootstrap", capability="partial")

    def install_browser(self, *, force: bool = False) -> CommandResult:
        return CommandResult(data=install_browser(self._paths, force=force), source="bootstrap", capability="partial")

    def doctor(self) -> CommandResult:
        snapshot = self.snapshot()
        checks = [
            {
                "name": "private_root_exists",
                "ok": self._paths.private_root.exists(),
                "detail": str(self._paths.private_root),
            },
            {
                "name": "config_exists",
                "ok": self._paths.config_path.exists(),
                "detail": str(self._paths.config_path),
            },
            {
                "name": "profile_exists",
                "ok": self._paths.profile_dir.exists(),
                "detail": str(self._paths.profile_dir),
            },
            {
                "name": "storage_state_exists",
                "ok": self._paths.storage_state_path.exists(),
                "detail": str(self._paths.storage_state_path),
            },
            {
                "name": "app_login_detection_enabled",
                "ok": True,
                "detail": ", ".join(APP_LOGIN_PATHS),
            },
        ]

        cookie_stats = snapshot.get("storage_state_cookie_stats")
        if isinstance(cookie_stats, dict) and "read_error" in cookie_stats:
            checks.append(
                {
                    "name": "storage_state_parseable",
                    "ok": False,
                    "detail": str(cookie_stats["read_error"]),
                }
            )
        elif cookie_stats is not None:
            checks.append(
                {
                    "name": "storage_state_parseable",
                    "ok": True,
                    "detail": json.dumps(cookie_stats, ensure_ascii=False),
                }
            )

        ok_count = sum(1 for check in checks if bool(check["ok"]))
        overall = "ok" if ok_count == len(checks) and snapshot["auth_mode"] != "none" else "warning"
        report = {
            "status": overall,
            "checks": checks,
            "auth_snapshot": snapshot,
        }
        return CommandResult(data=report, source="bootstrap", capability="partial")

    def _persist_context_state(self, context: Any) -> None:
        context.storage_state(path=str(self._paths.storage_state_path))
        chmod_best_effort(self._paths.storage_state_path, 0o600)

    def _manual_login(self, *, config: KlmsConfig) -> CommandResult:
        from playwright.sync_api import sync_playwright  # type: ignore[import-untyped]

        if sys.platform.startswith("linux") and not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
            raise CommandError(
                code="AUTH_FLOW_UNSUPPORTED",
                message="Manual browser login is not supported on headless Linux without a display server.",
                hint="Use `kaist klms auth login --username <KAIST_ID>` on headless Linux hosts.",
                exit_code=10,
                retryable=False,
            )
        if not sys.stdin or not sys.stdin.isatty():
            raise CommandError(
                code="AUTH_FLOW_UNSUPPORTED",
                message="Manual browser login requires an interactive terminal.",
                hint="Use `kaist klms auth login --username <KAIST_ID>` in non-interactive or headless shells.",
                exit_code=10,
                retryable=False,
            )
        print(f"Opening browser to: {config.base_url}", file=sys.stderr)
        print("Log in fully, navigate to a course page, then return here and press Enter.", file=sys.stderr)
        with _hold_profile_lock(self._paths):
            with sync_playwright() as playwright:
                context = _launch_chromium_persistent_context_sync(
                    playwright,
                    paths=self._paths,
                    user_data_dir=str(self._paths.profile_dir),
                    headless=False,
                    accept_downloads=False,
                )
                page = context.new_page()
                page.goto(config.base_url, wait_until="domcontentloaded", timeout=30_000)
                print("Press Enter to save session and exit... ", end="", file=sys.stderr, flush=True)
                input()
                self._persist_context_state(context)
                context.close()

        return CommandResult(
            data={
                "ok": True,
                "base_url": config.base_url,
                "dashboard_path": config.dashboard_path,
                "config_path": str(self._paths.config_path),
                "profile_path": str(self._paths.profile_dir),
                "storage_state_path": str(self._paths.storage_state_path),
                "preferred_mode": "profile",
                "login_strategy": "manual_browser",
            },
            source="browser",
            capability="full",
        )

    def _wait_for_easy_login_init(self, page: Any, *, timeout_seconds: float) -> str:
        deadline = time.monotonic() + max(1.0, timeout_seconds)
        while time.monotonic() < deadline:
            current_url = str(page.url or "")
            html = _safe_page_content(page)
            if html:
                error_message = _extract_easy_login_error_message(html)
                if error_message:
                    raise CommandError(
                        code="AUTH_FAILED",
                        message=f"KAIST Easy Login rejected the username ({error_message}).",
                        hint="Check the KAIST account ID and confirm Easy Login is registered in the KAIST auth app.",
                        exit_code=10,
                        retryable=False,
                    )
                if _extract_easy_login_number(html) or _looks_like_easy_login_verification_page(html):
                    return html
            if EASY_LOGIN_SUBMIT_NEEDLE in current_url or "/auth/twofactor/mfa/" in current_url:
                return html or ""
            if not _looks_like_easy_login_page(current_url):
                return html or ""
            page.wait_for_timeout(250)
        raise CommandError(
            code="AUTH_TIMEOUT",
            message="Timed out waiting for KAIST Easy Login to initialize.",
            hint="Try again, or fall back to `kaist klms auth login` without `--username`.",
            exit_code=10,
            retryable=True,
        )

    def _easy_login_success_result(self, *, config: KlmsConfig, username: str, login_number: str | None) -> CommandResult:
        return CommandResult(
            data={
                "ok": True,
                "base_url": config.base_url,
                "dashboard_path": config.dashboard_path,
                "config_path": str(self._paths.config_path),
                "profile_path": str(self._paths.profile_dir),
                "storage_state_path": str(self._paths.storage_state_path),
                "preferred_mode": "profile",
                "login_strategy": "sso_easy_login",
                "username": username,
                "login_number": login_number,
            },
            source="browser",
            capability="full",
        )

    def _context_has_authenticated_page(self, context: Any, *, config: KlmsConfig) -> bool:
        pages = getattr(context, "pages", None)
        if not isinstance(pages, list):
            return False
        return any(_page_has_authenticated_klms_session(page, config=config) for page in pages)

    def _wait_for_easy_login_approval(
        self,
        *,
        page: Any,
        context: Any,
        config: KlmsConfig,
        username: str,
        wait_seconds: float,
        login_number: str | None,
        signals: _EasyLoginSignals,
    ) -> CommandResult:
        auth_deadline = time.monotonic() + wait_seconds
        last_auth_check = 0.0
        policy_success_at: float | None = None
        submitted_link_form = False
        completed_device_registration = False
        printed_login_number = login_number

        while time.monotonic() < auth_deadline:
            now = time.monotonic()
            current_url = _safe_page_url(page) or ""
            lowered_url = current_url.lower()
            current_html = None
            if _looks_like_easy_login_page(current_url) or current_url.startswith(config.base_url.rstrip("/")):
                current_html = _safe_page_content(page)
            approved = False

            if _page_has_authenticated_klms_session(page, config=config) or self._context_has_authenticated_page(context, config=config):
                self._persist_context_state(context)
                return self._easy_login_success_result(config=config, username=username, login_number=printed_login_number)

            if "/auth/kaist/user/device/view" in lowered_url:
                if not completed_device_registration:
                    completed_device_registration = _complete_easy_login_device_registration(page)
                if not completed_device_registration:
                    raise CommandError(
                        code="AUTH_FLOW_UNSUPPORTED",
                        message="KAIST SSO requires device registration before Easy Login can complete.",
                        hint="Use the manual browser login flow once to complete the device-registration step.",
                        exit_code=10,
                        retryable=False,
                    )
                try:
                    page.wait_for_timeout(250)
                except Exception:
                    pass
                continue

            if current_html:
                error_message = _extract_easy_login_error_message(current_html)
                if error_message:
                    raise CommandError(
                        code="AUTH_FAILED",
                        message=f"KAIST Easy Login failed after initialization ({error_message}).",
                        hint="Try again, or fall back to `kaist klms auth login` without `--username`.",
                        exit_code=10,
                        retryable=False,
                    )
                current_number = _extract_easy_login_number(current_html)
                if _should_update_easy_login_number(previous=printed_login_number, current=current_number):
                    printed_login_number = current_number
                    print(f"KAIST SSO Easy Login number: {current_number}", file=sys.stderr)

            if signals.latest_mfa_payload is not None:
                auth_state = _evaluate_easy_login_mfa_payload(signals.latest_mfa_payload)
                if auth_state == "approved":
                    approved = True
                    if signals.latest_policy_payload is not None:
                        policy_state = _evaluate_easy_login_policy_payload(signals.latest_policy_payload)
                        if policy_state == "success" and policy_success_at is None:
                            policy_success_at = now
                        elif policy_state == "device_registration":
                            approved = True

            if (
                policy_success_at is not None
                and not submitted_link_form
                and _looks_like_easy_login_page(current_url)
                and now - policy_success_at >= 0.5
            ):
                submitted_link_form = _submit_easy_login_link(page)

            if approved and not _looks_like_easy_login_page(current_url):
                state = self._context_dashboard_state(context, config=config, timeout_ms=5_000)
                if state["authenticated"]:
                    self._persist_context_state(context)
                    return self._easy_login_success_result(config=config, username=username, login_number=printed_login_number)

            if now - last_auth_check >= EASY_LOGIN_POLL_SECONDS:
                last_auth_check = now
                state = self._context_dashboard_state(context, config=config, timeout_ms=5_000)
                if state["authenticated"]:
                    self._persist_context_state(context)
                    return self._easy_login_success_result(config=config, username=username, login_number=printed_login_number)

            try:
                page.wait_for_timeout(int(EASY_LOGIN_POLL_SECONDS * 1000))
            except Exception:
                if self._context_has_authenticated_page(context, config=config):
                    self._persist_context_state(context)
                    return self._easy_login_success_result(config=config, username=username, login_number=printed_login_number)
                state = self._context_dashboard_state(context, config=config, timeout_ms=5_000)
                if state["authenticated"]:
                    self._persist_context_state(context)
                    return self._easy_login_success_result(config=config, username=username, login_number=printed_login_number)
                time.sleep(max(EASY_LOGIN_POLL_SECONDS, 0.1))

        detail = f" Login number: {printed_login_number}." if printed_login_number else ""
        raise CommandError(
            code="AUTH_TIMEOUT",
            message=f"Timed out waiting for KAIST Easy Login approval.{detail}",
            hint="Approve the request in the KAIST auth app faster, or use `kaist klms auth login` without `--username`.",
            exit_code=10,
            retryable=True,
        )

    def _easy_login(self, *, config: KlmsConfig, username: str, wait_seconds: float) -> CommandResult:
        from playwright.sync_api import sync_playwright  # type: ignore[import-untyped]

        wait_seconds = max(15.0, min(float(wait_seconds), 600.0))
        printed_login_number: str | None = None
        with _hold_profile_lock(self._paths):
            with sync_playwright() as playwright:
                context = _launch_chromium_persistent_context_sync(
                    playwright,
                    paths=self._paths,
                    user_data_dir=str(self._paths.profile_dir),
                    headless=True,
                    accept_downloads=False,
                )
                signals = _EasyLoginSignals()
                response_listener = lambda response: _observe_easy_login_response(signals, response)  # noqa: E731
                try:
                    page = context.new_page()
                    context.on("response", response_listener)
                    page.goto(config.base_url, wait_until="domcontentloaded", timeout=30_000)
                    html = _safe_page_content(page) or ""
                    current_url = str(page.url or "")
                    sso_url = _extract_sso_login_view_url(current_url, html)
                    if not sso_url:
                        if _looks_like_easy_login_page(current_url):
                            sso_url = current_url
                        else:
                            raise CommandError(
                                code="AUTH_FLOW_UNSUPPORTED",
                                message="Could not locate the KAIST SSO Easy Login page from the KLMS login flow.",
                                hint="Run `kaist klms auth login` without `--username` to use the manual browser flow.",
                                exit_code=10,
                                retryable=False,
                            )
                    page.goto(sso_url, wait_until="networkidle", timeout=30_000)
                    page.fill("#login_id_mfa", username)
                    page.click("a.btn_login")
                    html = self._wait_for_easy_login_init(page, timeout_seconds=12.0)
                    login_number = _extract_easy_login_number(html)
                    if login_number:
                        printed_login_number = login_number
                        print(f"KAIST SSO Easy Login number: {login_number}", file=sys.stderr)
                        print("Approve this login in the KAIST auth app. Waiting for KLMS session...", file=sys.stderr)
                    else:
                        print("KAIST SSO Easy Login initialized. Waiting for approval in the KAIST auth app...", file=sys.stderr)
                    return self._wait_for_easy_login_approval(
                        page=page,
                        context=context,
                        config=config,
                        username=username,
                        wait_seconds=wait_seconds,
                        login_number=printed_login_number,
                        signals=signals,
                    )
                finally:
                    try:
                        context.remove_listener("response", response_listener)
                    except Exception:
                        pass
                    context.close()

    def login(
        self,
        *,
        base_url: str | None = None,
        dashboard_path: str | None = None,
        username: str | None = None,
        wait_seconds: float = EASY_LOGIN_DEFAULT_WAIT_SECONDS,
    ) -> CommandResult:
        normalized_username = str(username or "").strip()
        config = save_config(
            self._paths,
            base_url=base_url,
            dashboard_path=dashboard_path,
            auth_username=normalized_username if normalized_username else None,
        )
        configure_playwright_env(self._paths)
        self._paths.profile_dir.mkdir(parents=True, exist_ok=True)
        chmod_best_effort(self._paths.profile_dir, 0o700)
        if normalized_username:
            return self._easy_login(config=config, username=normalized_username, wait_seconds=wait_seconds)
        return self._manual_login(config=config)

    def refresh(
        self,
        *,
        base_url: str | None = None,
        dashboard_path: str | None = None,
        username: str | None = None,
        wait_seconds: float = EASY_LOGIN_DEFAULT_WAIT_SECONDS,
    ) -> CommandResult:
        existing = maybe_load_config(self._paths)
        resolved_username = str(username or "").strip() or (existing.auth_username if existing else None)
        return self.login(
            base_url=base_url,
            dashboard_path=dashboard_path,
            username=resolved_username,
            wait_seconds=wait_seconds,
        )

    def _context_dashboard_state(self, context: Any, *, config: KlmsConfig, timeout_ms: int) -> dict[str, Any]:
        page = context.new_page()
        try:
            page.goto(config.base_url.rstrip("/") + config.dashboard_path, wait_until="domcontentloaded", timeout=timeout_ms)
            html = page.content()
            final_url = page.url
        finally:
            page.close()
        return {
            "final_url": final_url,
            "html": html,
            "authenticated": not looks_login_url(final_url) and not looks_logged_out_html(html),
            "login_url_detected": looks_login_url(final_url),
            "login_html_detected": looks_logged_out_html(html),
        }

    def run_authenticated(
        self,
        *,
        config: KlmsConfig,
        headless: bool,
        accept_downloads: bool,
        timeout_seconds: float,
        callback: Any,
    ) -> Any:
        return self._run_authenticated_internal(
            config=config,
            headless=headless,
            accept_downloads=accept_downloads,
            timeout_seconds=timeout_seconds,
            callback=callback,
            include_dashboard_state=False,
        )

    def run_authenticated_with_state(
        self,
        *,
        config: KlmsConfig,
        headless: bool,
        accept_downloads: bool,
        timeout_seconds: float,
        callback: Any,
    ) -> Any:
        return self._run_authenticated_internal(
            config=config,
            headless=headless,
            accept_downloads=accept_downloads,
            timeout_seconds=timeout_seconds,
            callback=callback,
            include_dashboard_state=True,
        )

    def _run_authenticated_internal(
        self,
        *,
        config: KlmsConfig,
        headless: bool,
        accept_downloads: bool,
        timeout_seconds: float,
        callback: Any,
        include_dashboard_state: bool,
    ) -> Any:
        if active_auth_mode(self._paths) == "none":
            raise CommandError(
                code="AUTH_MISSING",
                message="No saved KLMS auth artifacts were found.",
                hint="Run `kaist klms auth login --base-url https://klms.kaist.ac.kr` first.",
                exit_code=10,
                retryable=True,
            )

        configure_playwright_env(self._paths)
        timeout_ms = max(1_000, int(timeout_seconds * 1000))
        attempts: list[dict[str, Any]] = []
        profile_session_exists = has_profile_session(self._paths)
        profile_lock = _hold_profile_lock(self._paths) if profile_session_exists else nullcontext()

        with profile_lock:
            from playwright.sync_api import sync_playwright  # type: ignore[import-untyped]

            with sync_playwright() as playwright:
                if profile_session_exists:
                    try:
                        profile_context = _launch_chromium_persistent_context_sync(
                            playwright,
                            paths=self._paths,
                            user_data_dir=str(self._paths.profile_dir),
                            headless=headless,
                            accept_downloads=accept_downloads,
                        )
                    except Exception as exc:  # noqa: BLE001
                        attempts.append({"auth_mode": "profile", "launch_error": str(exc)})
                    else:
                        try:
                            state = self._context_dashboard_state(profile_context, config=config, timeout_ms=timeout_ms)
                            attempts.append({"auth_mode": "profile", **state})
                            if state["authenticated"]:
                                if include_dashboard_state:
                                    return callback(profile_context, "profile", state)
                                return callback(profile_context, "profile")
                        finally:
                            profile_context.close()

                if has_storage_state_session(self._paths):
                    try:
                        browser = _launch_chromium_browser_sync(playwright, paths=self._paths, headless=headless)
                        storage_context = browser.new_context(
                            storage_state=str(self._paths.storage_state_path),
                            accept_downloads=accept_downloads,
                        )
                    except Exception as exc:  # noqa: BLE001
                        attempts.append({"auth_mode": "storage_state", "launch_error": str(exc)})
                    else:
                        try:
                            state = self._context_dashboard_state(storage_context, config=config, timeout_ms=timeout_ms)
                            attempts.append({"auth_mode": "storage_state", **state})
                            if state["authenticated"]:
                                if include_dashboard_state:
                                    return callback(storage_context, "storage_state", state)
                                return callback(storage_context, "storage_state")
                        finally:
                            storage_context.close()
                            browser.close()

        attempt_summaries = [
            (
                f"{attempt['auth_mode']}: launch_error={attempt.get('launch_error')}"
                if attempt.get("launch_error")
                else f"{attempt['auth_mode']}: final_url={attempt.get('final_url')}"
            )
            for attempt in attempts
        ]
        detail = "; ".join(attempt_summaries) if attempt_summaries else "no attempts"
        raise CommandError(
            code="AUTH_EXPIRED",
            message=f"Saved KLMS auth did not reach an authenticated dashboard session ({detail}).",
            hint="Run `kaist klms auth refresh` and complete the login flow again.",
            exit_code=10,
            retryable=True,
        )

    def browser_probe(self, *, config: KlmsConfig, timeout_seconds: float, recent_courses_args: dict[str, Any] | None) -> dict[str, Any]:
        configure_playwright_env(self._paths)
        timeout_ms = max(1_000, int(timeout_seconds * 1000))

        from playwright.sync_api import sync_playwright  # type: ignore[import-untyped]

        def probe_context(context: Any, *, auth_mode: str) -> dict[str, Any]:
            page = context.new_page()
            try:
                try:
                    page.goto(config.base_url.rstrip("/") + config.dashboard_path, wait_until="domcontentloaded", timeout=timeout_ms)
                    html = page.content()
                    final_url = page.url
                except Exception as exc:  # noqa: BLE001
                    return {
                        "auth_mode": auth_mode,
                        "dashboard_authenticated": False,
                        "status": "error",
                        "error": str(exc),
                        "sesskey_detected": False,
                        "ajax_recent_courses": {
                            "status": "skipped",
                            "reason": "Dashboard load failed.",
                        },
                    }

                authenticated = not looks_login_url(final_url) and not looks_logged_out_html(html)
                result: dict[str, Any] = {
                    "auth_mode": auth_mode,
                    "dashboard_final_url": final_url,
                    "dashboard_authenticated": authenticated,
                    "dashboard_login_url_detected": looks_login_url(final_url),
                    "dashboard_login_html_detected": looks_logged_out_html(html),
                    "sesskey_detected": False,
                    "ajax_recent_courses": {
                        "status": "skipped",
                        "reason": "No authenticated dashboard session.",
                    },
                }
                if not authenticated:
                    return result

                sesskey = extract_sesskey(html)
                result["sesskey_detected"] = bool(sesskey)
                if not sesskey:
                    result["ajax_recent_courses"] = {
                        "status": "skipped",
                        "reason": "Authenticated page did not expose sesskey.",
                    }
                    return result

                args = dict(recent_courses_args or {})
                payload = [{"index": 0, "methodname": "core_course_get_recent_courses", "args": args}]
                ajax_url = f"{config.base_url.rstrip('/')}/lib/ajax/service.php?sesskey={sesskey}&info=core_course_get_recent_courses"
                ajax_result = page.evaluate(
                    """
                    async ({url, payload}) => {
                      const response = await fetch(url, {
                        method: "POST",
                        headers: {
                          "Content-Type": "application/json",
                          "X-Requested-With": "XMLHttpRequest",
                          "Accept": "application/json, text/javascript, */*; q=0.01"
                        },
                        body: JSON.stringify(payload),
                        credentials: "same-origin"
                      });
                      const text = await response.text();
                      return {
                        ok: response.ok,
                        status: response.status,
                        url: response.url,
                        contentType: response.headers.get("content-type") || "",
                        text
                      };
                    }
                    """,
                    {"url": ajax_url, "payload": payload},
                )
                ajax_text = str(ajax_result.get("text") or "")
                ajax_report: dict[str, Any] = {
                    "status": "ok" if ajax_result.get("ok") else "error",
                    "http_status": ajax_result.get("status"),
                    "final_url": ajax_result.get("url"),
                    "content_type": ajax_result.get("contentType"),
                    "preview": ajax_text[:400],
                    "args_used": args,
                }
                try:
                    parsed = json.loads(ajax_text)
                except Exception as exc:  # noqa: BLE001
                    ajax_report["json_parse_ok"] = False
                    ajax_report["parse_error"] = str(exc)
                else:
                    ajax_report["json_parse_ok"] = True
                    ajax_report["payload_kind"] = type(parsed).__name__
                    if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
                        ajax_report["ajax_error"] = bool(parsed[0].get("error"))
                        data = parsed[0].get("data")
                        if isinstance(data, list):
                            ajax_report["course_count"] = len(data)
                            if data and isinstance(data[0], dict):
                                ajax_report["sample_course"] = {
                                    "id": data[0].get("id"),
                                    "fullname": data[0].get("fullname"),
                                    "shortname": data[0].get("shortname"),
                                    "viewurl": data[0].get("viewurl"),
                                }
                result["ajax_recent_courses"] = ajax_report
                return result
            finally:
                page.close()

        if active_auth_mode(self._paths) == "none":
            return {
                "enabled": True,
                "status": "skipped",
                "reason": "No saved auth artifacts available for browser-assisted probing.",
            }

        profile_session_exists = has_profile_session(self._paths)
        profile_lock = _hold_profile_lock(self._paths) if profile_session_exists else nullcontext()

        with profile_lock:
            from playwright.sync_api import sync_playwright  # type: ignore[import-untyped]

            with sync_playwright() as playwright:
                if profile_session_exists:
                    profile_context = _launch_chromium_persistent_context_sync(
                        playwright,
                        paths=self._paths,
                        user_data_dir=str(self._paths.profile_dir),
                        headless=True,
                        accept_downloads=False,
                    )
                    try:
                        profile_result = probe_context(profile_context, auth_mode="profile")
                        if profile_result.get("dashboard_authenticated"):
                            return {
                                "enabled": True,
                                "status": "ok",
                                "attempts": [profile_result],
                                "selected_auth_mode": "profile",
                            }
                    finally:
                        profile_context.close()
                else:
                    profile_result = None

                if has_storage_state_session(self._paths):
                    browser = _launch_chromium_browser_sync(playwright, paths=self._paths, headless=True)
                    storage_context = browser.new_context(
                        storage_state=str(self._paths.storage_state_path),
                        accept_downloads=False,
                    )
                    try:
                        storage_result = probe_context(storage_context, auth_mode="storage_state")
                        return {
                            "enabled": True,
                            "status": "ok" if storage_result.get("dashboard_authenticated") else "warning",
                            "attempts": [attempt for attempt in [profile_result, storage_result] if attempt is not None],
                            "selected_auth_mode": "storage_state" if storage_result.get("dashboard_authenticated") else None,
                        }
                    finally:
                        storage_context.close()
                        browser.close()

                return {
                    "enabled": True,
                    "status": "warning",
                    "attempts": [attempt for attempt in [profile_result] if attempt is not None],
                    "selected_auth_mode": None,
                }
