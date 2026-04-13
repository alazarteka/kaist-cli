from __future__ import annotations

import html as _html
import getpass
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import traceback
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urljoin
from secrets import token_hex

from bs4 import BeautifulSoup  # type: ignore[import-untyped]

from ...core.state_store import file_lock
from ..contracts import CommandError, CommandResult
from .auth_session import clear_auth_session, load_auth_session, new_auth_session_id, save_auth_session, session_expiry_iso, utc_now_iso
from .config import KlmsConfig, maybe_load_config, save_config
from .paths import KlmsPaths, chmod_best_effort, configure_playwright_env, ensure_private_dirs
from .secrets import KeychainSecretStore


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
EMAIL_OTP_DEFAULT_WAIT_SECONDS = 180.0
EMAIL_OTP_SESSION_TTL_SECONDS = 10 * 60
EMAIL_OTP_WORKER_READY_TIMEOUT_SECONDS = 45.0
EMAIL_OTP_WORKER_POLL_SECONDS = 0.25
AUTH_SESSION_STARTING_GRACE_SECONDS = 30.0
AUTH_STATUS_REFRESH_WINDOW_HOURS = 3.0


def epoch_to_iso_utc(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _parse_iso_utc(value: str | None) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


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


def _looks_like_email_otp_page(url: str, html: str) -> bool:
    lowered_url = (url or "").lower()
    lowered_html = (html or "").lower()
    url_markers = ("otp", "email", "mail", "verify", "cert", "2factor")
    text_markers = (
        "verification code",
        "one-time code",
        "one time code",
        "email verification",
        "sent to your email",
        "sent to email",
        "인증번호",
        "이메일",
        "메일",
        "보안코드",
        "otp",
    )
    has_input = bool(re.search(r"<input[^>]+(?:type=['\"]?(?:text|tel|number)['\"]?)", lowered_html))
    return (any(marker in lowered_url for marker in url_markers) or any(marker in lowered_html for marker in text_markers)) and has_input


def _extract_email_otp_error_message(html: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    for selector in ("#otpResultMsg", "#resultMsg", "#mfaResultMsg", ".alert-danger", ".error", ".txt_error"):
        node = soup.select_one(selector)
        if node is None:
            continue
        text = " ".join(node.get_text(" ", strip=True).split())
        if text:
            return text
    return None


def _submit_password_login(page: Any, *, username: str, password: str) -> bool:
    script = """
    ([username, password]) => {
      const visible = (node) => {
        if (!node) return false;
        const style = window.getComputedStyle(node);
        return style.display !== 'none' && style.visibility !== 'hidden';
      };
      const clickIfPresent = (selector) => {
        const node = document.querySelector(selector);
        if (node) node.click();
      };
      clickIfPresent('li#auth a');
      clickIfPresent('#auth a');

      const activePane = document.querySelector('#loginTab02');
      if (activePane && !visible(activePane)) {
        activePane.style.display = 'block';
      }
      const inactivePane = document.querySelector('#loginTab01');
      if (inactivePane && visible(activePane)) {
        inactivePane.style.display = 'none';
      }

      const findFirst = (selectors, root = document) => {
        for (const selector of selectors) {
          const nodes = Array.from(root.querySelectorAll(selector));
          const preferred = nodes.find((node) => visible(node)) || nodes[0];
          if (preferred) return preferred;
        }
        return null;
      };

      const passwordRoot = activePane || document;
      const userInput = findFirst([
        '#login_id', '#loginId', '#username', '#userId', 'input[name="login_id"]',
        'input[name="loginId"]', 'input[name="username"]', 'input[name="userId"]',
        '#login_id_mfa', 'input[name="login_id_mfa"]',
        'input[type="text"][name*="id"]', 'input[type="email"]'
      ], passwordRoot);
      const passInput = findFirst([
        '#login_pwd', '#login_pw', '#loginPw', '#password', '#userPw', 'input[name="login_pwd"]',
        'input[name="login_pw"]', 'input[name="loginPw"]', 'input[name="password"]', 'input[name="userPw"]',
        'input[type="password"]'
      ], passwordRoot);
      if (!userInput || !passInput) return false;
      userInput.focus();
      userInput.value = username;
      userInput.dispatchEvent(new Event('input', { bubbles: true }));
      userInput.dispatchEvent(new Event('change', { bubbles: true }));
      passInput.focus();
      passInput.value = password;
      passInput.dispatchEvent(new Event('input', { bubbles: true }));
      passInput.dispatchEvent(new Event('change', { bubbles: true }));
      const submit = findFirst([
        'button[type="submit"]', 'input[type="submit"]', 'a.btn_login', '.btn_login'
      ], passwordRoot);
      if (submit) {
        submit.click();
        return true;
      }
      if (passInput.form) {
        passInput.form.submit();
        return true;
      }
      return false;
    }
    """
    try:
        return bool(page.evaluate(script, [username, password]))
    except Exception:
        return False


def _submit_email_otp_code(page: Any, *, otp: str) -> bool:
    script = """
    (otp) => {
      const visible = (node) => {
        if (!node) return false;
        const style = window.getComputedStyle(node);
        return style.display !== 'none' && style.visibility !== 'hidden';
      };
      if (typeof window.send_flag !== 'undefined') {
        window.send_flag = true;
      }
      const otpInput = document.querySelector('#crtfc_no');
      const submitButton = document.querySelector('#proc');
      if (otpInput) otpInput.disabled = false;
      if (submitButton) submitButton.disabled = false;
      const authWrap = document.querySelector('.factor_auth_wrap');
      if (authWrap) authWrap.classList.remove('disable');
      const inputs = Array.from(document.querySelectorAll('input'))
        .filter((node) => visible(node))
        .filter((node) => !['hidden', 'password', 'submit', 'button'].includes((node.type || '').toLowerCase()));
      const splitInputs = inputs.filter((node) => ['1', 1].includes(node.maxLength));
      if (splitInputs.length === otp.length && splitInputs.length > 1) {
        splitInputs.forEach((node, index) => {
          node.focus();
          node.value = otp[index] || '';
          node.dispatchEvent(new Event('input', { bubbles: true }));
          node.dispatchEvent(new Event('change', { bubbles: true }));
        });
      } else {
        const candidate = document.querySelector('#crtfc_no') || inputs.find((node) => {
          const hint = [node.id, node.name, node.className].join(' ').toLowerCase();
          return ['otp', 'code', 'auth', 'verify', 'cert'].some((token) => hint.includes(token));
        }) || inputs[0];
        if (!candidate) return false;
        candidate.focus();
        candidate.value = otp;
        candidate.dispatchEvent(new Event('input', { bubbles: true }));
        candidate.dispatchEvent(new Event('change', { bubbles: true }));
      }
      const buttons = Array.from(document.querySelectorAll('button, input[type="submit"], a'));
      const submit = buttons.find((node) => {
        const text = ((node.textContent || '') + ' ' + (node.value || '') + ' ' + (node.id || '') + ' ' + (node.className || '')).toLowerCase();
        return ['submit', 'verify', 'confirm', 'continue', '인증', '확인', '로그인'].some((token) => text.includes(token));
      });
      if (submit) {
        submit.click();
        return true;
      }
      const form = inputs[0]?.form;
      if (form) {
        form.submit();
        return true;
      }
      return false;
    }
    """
    try:
        return bool(page.evaluate(script, otp))
    except Exception:
        return False


def _request_email_otp_delivery(page: Any) -> bool:
    script = """
    () => {
      const visible = (node) => {
        if (!node) return false;
        const style = window.getComputedStyle(node);
        return style.display !== 'none' && style.visibility !== 'hidden';
      };
      const button = document.querySelector('#email')
        || Array.from(document.querySelectorAll('input[type="submit"], button, a')).find((node) => {
          const text = ((node.textContent || '') + ' ' + (node.value || '') + ' ' + (node.id || '')).toLowerCase();
          return ['mail', 'email', '외부 메일'].some((token) => text.includes(token));
        });
      if (!button || !visible(button)) return false;
      button.click();
      return true;
    }
    """
    try:
        return bool(page.evaluate(script))
    except Exception:
        return False


def _response_json_object(response: Any) -> dict[str, Any] | None:
    try:
        payload = response.json()
    except Exception:
        try:
            payload = json.loads(str(response.text() or ""))
        except Exception:
            return None
    return payload if isinstance(payload, dict) else None


def _command_error_payload(exc: CommandError) -> dict[str, Any]:
    return {
        "code": exc.code,
        "message": exc.message,
        "hint": exc.hint,
        "exit_code": exc.exit_code,
        "retryable": exc.retryable,
    }


def _raise_command_error_payload(payload: dict[str, Any]) -> None:
    raise CommandError(
        code=str(payload.get("code") or "AUTH_FAILED"),
        message=str(payload.get("message") or "KLMS auth worker failed."),
        hint=str(payload.get("hint") or "").strip() or None,
        exit_code=int(payload.get("exit_code") or 10),
        retryable=bool(payload.get("retryable")),
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


def _latest_auth_artifact_mtime(paths: KlmsPaths) -> float | None:
    mtimes: list[float] = []
    for candidate in (paths.storage_state_path,):
        try:
            if candidate.exists():
                mtimes.append(candidate.stat().st_mtime)
        except OSError:
            continue
    try:
        if paths.profile_dir.exists():
            mtimes.append(paths.profile_dir.stat().st_mtime)
            for child in paths.profile_dir.rglob("*"):
                try:
                    mtimes.append(child.stat().st_mtime)
                except OSError:
                    continue
    except OSError:
        pass
    if not mtimes:
        return None
    return max(mtimes)


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
    def __init__(self, paths: KlmsPaths, *, secret_store: KeychainSecretStore | None = None) -> None:
        self._paths = paths
        self._secret_store = secret_store or KeychainSecretStore()

    def _email_otp_secret_command(self, username: str | None) -> str:
        suffix = f" --username {username}" if username else ""
        return f"`kaist klms auth store-email-otp-secret{suffix}`"

    def _config_payload(self, config: KlmsConfig | None) -> dict[str, Any] | None:
        if config is None:
            return None
        payload = {
            "base_url": config.base_url,
            "dashboard_path": config.dashboard_path,
            "auth_username": config.auth_username,
            "auth_strategy": config.auth_strategy,
            "otp_source": config.otp_source,
            "course_ids": list(config.course_ids),
            "notice_board_ids": list(config.notice_board_ids),
            "exclude_course_title_patterns": list(config.exclude_course_title_patterns),
        }
        if config.auth_strategy == "email_otp":
            payload["secret_storage"] = "macos_keychain"
        return payload

    def _recommended_action(
        self,
        *,
        config: KlmsConfig | None,
        mode: AuthMode,
        staged_auth_session: dict[str, Any] | None,
    ) -> str:
        if staged_auth_session is not None:
            stage = str(staged_auth_session.get("stage") or "").strip()
            session_id = str(staged_auth_session.get("session_id") or "").strip()
            if stage == "waiting_for_email_otp" and session_id:
                return f"Run `kaist klms auth complete-refresh {session_id} --otp CODE`, or `kaist klms auth cancel-refresh {session_id}` to abort."
            if stage == "failed":
                return "Run `kaist klms auth begin-refresh` again to create a fresh email OTP session."
        if config is None:
            return "Run `kaist klms auth login --base-url https://klms.kaist.ac.kr`."
        if config.auth_strategy == "email_otp":
            return (
                "Run `kaist klms auth refresh` to start an email OTP session. "
                f"If the password has not been stored yet, run {self._email_otp_secret_command(config.auth_username)} in a separate terminal first."
            )
        if mode == "none":
            return "Run `kaist klms auth login` to create a persistent browser profile."
        return "Run `kaist klms auth refresh` if the saved session stops working."

    def _stale_auth_session_reason(self, payload: dict[str, Any]) -> str | None:
        stage = str(payload.get("stage") or "").strip()
        expires_at = _parse_iso_utc(payload.get("expires_at"))
        now = datetime.now(timezone.utc)
        if expires_at is not None and expires_at <= now:
            return "expired"
        if stage in {"completed", "canceled"}:
            return stage
        worker_pid = int(payload.get("worker_pid") or 0)
        if stage == "starting" and worker_pid > 0 and not _pid_is_running(worker_pid):
            return "worker_exited"
        started_at = _parse_iso_utc(payload.get("started_at"))
        if stage == "starting" and worker_pid <= 0 and started_at is not None:
            age = (now - started_at).total_seconds()
            if age >= AUTH_SESSION_STARTING_GRACE_SECONDS:
                return "startup_stalled"
        return None

    def _cleanup_stale_auth_session(self, payload: dict[str, Any] | None) -> dict[str, Any] | None:
        if payload is None:
            return None
        if self._stale_auth_session_reason(payload) is None:
            return payload
        clear_auth_session(self._paths)
        return None

    def _load_current_auth_session(self) -> dict[str, Any] | None:
        return self._cleanup_stale_auth_session(load_auth_session(self._paths))

    def _read_auth_worker_log_tail(self, *, max_lines: int = 20) -> str:
        try:
            text = self._paths.auth_worker_log_path.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError):
            return ""
        return _tail_text(text, max_lines=max_lines)

    def _refresh_heuristic(self) -> dict[str, Any] | None:
        last_refresh_epoch = _latest_auth_artifact_mtime(self._paths)
        if last_refresh_epoch is None:
            return None
        refresh_due_epoch = last_refresh_epoch + (AUTH_STATUS_REFRESH_WINDOW_HOURS * 3600)
        now_epoch = time.time()
        remaining_hours = round((refresh_due_epoch - now_epoch) / 3600, 2)
        return {
            "kind": "fixed_window_since_last_refresh",
            "window_hours": AUTH_STATUS_REFRESH_WINDOW_HOURS,
            "last_refresh_at": epoch_to_iso_utc(last_refresh_epoch),
            "refresh_due_at": epoch_to_iso_utc(refresh_due_epoch),
            "refresh_overdue": refresh_due_epoch <= now_epoch,
            "hours_until_refresh": remaining_hours,
        }

    def snapshot(self) -> dict[str, Any]:
        ensure_private_dirs(self._paths)
        config = maybe_load_config(self._paths)
        mode = active_auth_mode(self._paths)
        staged_auth_session = self._session_snapshot()
        return {
            "configured": config is not None,
            "config_path": str(self._paths.config_path),
            "profile_path": str(self._paths.profile_dir),
            "storage_state_path": str(self._paths.storage_state_path),
            "auth_session_path": str(self._paths.auth_session_path),
            "auth_mode": mode,
            "session_artifacts": {
                "profile": has_profile_session(self._paths),
                "storage_state": has_storage_state_session(self._paths),
            },
            "refresh_heuristic": self._refresh_heuristic(),
            "storage_state_cookie_stats": storage_state_cookie_stats(self._paths),
            "staged_auth_session": staged_auth_session,
            "config": self._config_payload(config),
            "validation_mode": "offline-only",
            "login_detection": {
                "html_markers": list(HTML_LOGIN_MARKERS),
                "url_needles": list(URL_LOGIN_NEEDLES),
                "app_login_paths": list(APP_LOGIN_PATHS),
            },
            "recommended_action": self._recommended_action(config=config, mode=mode, staged_auth_session=staged_auth_session),
        }

    def _session_snapshot(self) -> dict[str, Any] | None:
        payload = self._load_current_auth_session()
        if payload is None:
            return None
        snapshot = {
            "session_id": str(payload.get("session_id") or "").strip() or None,
            "strategy": str(payload.get("strategy") or "").strip() or None,
            "stage": str(payload.get("stage") or "").strip() or None,
            "username": str(payload.get("username") or "").strip() or None,
            "started_at": str(payload.get("started_at") or "").strip() or None,
            "expires_at": str(payload.get("expires_at") or "").strip() or None,
            "otp_source": str(payload.get("otp_source") or "").strip() or None,
            "finished_at": str(payload.get("finished_at") or "").strip() or None,
            "challenge_url": str(payload.get("challenge_url") or "").strip() or None,
        }
        error = payload.get("error")
        if isinstance(error, dict):
            snapshot["error"] = error
        worker_pid = int(payload.get("worker_pid") or 0)
        if worker_pid > 0:
            snapshot["worker"] = {
                "pid": worker_pid,
                "running": _pid_is_running(worker_pid),
                "transport": "localhost",
            }
        return snapshot

    def status(self) -> CommandResult:
        return CommandResult(data=self.snapshot(), source="bootstrap", capability="partial")

    def install_browser(self, *, force: bool = False) -> CommandResult:
        return CommandResult(data=install_browser(self._paths, force=force), source="bootstrap", capability="partial")

    def _resolve_password_input(self, *, username: str, password_env: str | None) -> str:
        env_name = str(password_env or "").strip()
        if env_name:
            value = os.environ.get(env_name, "")
            if value:
                return value
            raise CommandError(
                code="CONFIG_INVALID",
                message=f"Environment variable {env_name} was empty or unset.",
                hint="Set the variable first, or rerun without `--password-env` to enter the password interactively.",
                exit_code=40,
                retryable=False,
            )
        if not sys.stdin or not sys.stdin.isatty():
            raise CommandError(
                code="AUTH_SECRET_UNAVAILABLE",
                message="Email OTP setup requires an interactive terminal or `--password-env`.",
                hint=f"Run {self._email_otp_secret_command(username)} in a human-run terminal, or use `--password-env VAR` in that command.",
                exit_code=10,
                retryable=False,
            )
        password = getpass.getpass(f"KAIST password for {username}: ")
        if not password.strip():
            raise CommandError(
                code="CONFIG_INVALID",
                message="Password must not be empty.",
                hint=f"Rerun {self._email_otp_secret_command(username)} and enter the KAIST password.",
                exit_code=40,
                retryable=False,
            )
        return password

    def _resolve_email_otp_secret_username(
        self,
        *,
        username: str | None = None,
        require_email_otp_strategy: bool = True,
    ) -> tuple[KlmsConfig | None, str]:
        config = maybe_load_config(self._paths)
        resolved_username = str(username or "").strip() or (config.auth_username if config else None)
        if require_email_otp_strategy and (config is None or config.auth_strategy != "email_otp"):
            raise CommandError(
                code="AUTH_FLOW_UNSUPPORTED",
                message="KLMS auth is not configured for email OTP secret storage.",
                hint="Run `kaist klms auth setup-email-otp --username <KAIST_ID>` first.",
                exit_code=10,
                retryable=False,
            )
        if not resolved_username:
            raise CommandError(
                code="CONFIG_INVALID",
                message="KAIST username is required for email OTP secret storage.",
                hint="Pass `--username <KAIST_ID>`, or configure email OTP auth first.",
                exit_code=40,
                retryable=False,
            )
        return config, resolved_username

    def setup_email_otp(
        self,
        *,
        base_url: str | None = None,
        dashboard_path: str | None = None,
        username: str,
        otp_source: str = "manual",
        password_env: str | None = None,
        prompt_password: bool = False,
    ) -> CommandResult:
        normalized_username = str(username or "").strip()
        if not normalized_username:
            raise CommandError(code="CONFIG_INVALID", message="KAIST username is required.", exit_code=40)
        config = save_config(
            self._paths,
            base_url=base_url,
            dashboard_path=dashboard_path,
            auth_username=normalized_username,
            auth_strategy="email_otp",
            otp_source=otp_source,
        )
        secret_configured = False
        if prompt_password or str(password_env or "").strip():
            password = self._resolve_password_input(username=normalized_username, password_env=password_env)
            self._secret_store.store_email_otp_password(username=normalized_username, password=password)
            secret_configured = True
        return CommandResult(
            data={
                "ok": True,
                "base_url": config.base_url,
                "dashboard_path": config.dashboard_path,
                "config_path": str(self._paths.config_path),
                "auth_strategy": config.auth_strategy,
                "otp_source": config.otp_source,
                "username": normalized_username,
                "secret_storage": "macos_keychain",
                "secret_configured": secret_configured,
                "next_step": (
                    "Email OTP setup is complete."
                    if secret_configured
                    else (
                        f"Run {self._email_otp_secret_command(normalized_username)} in a separate terminal to store the KAIST password in macOS Keychain."
                    )
                ),
            },
            source="bootstrap",
            capability="partial",
        )

    def store_email_otp_secret(
        self,
        *,
        username: str | None = None,
        password_env: str | None = None,
    ) -> CommandResult:
        _, resolved_username = self._resolve_email_otp_secret_username(username=username, require_email_otp_strategy=True)
        password = self._resolve_password_input(username=resolved_username, password_env=password_env)
        self._secret_store.store_email_otp_password(username=resolved_username, password=password)
        return CommandResult(
            data={
                "ok": True,
                "username": resolved_username,
                "secret_storage": "macos_keychain",
                "auth_strategy": "email_otp",
                "next_step": "Run `kaist klms auth refresh` to start the email OTP login flow.",
            },
            source="bootstrap",
            capability="partial",
        )

    def clear_email_otp_secret(self, *, username: str | None = None) -> CommandResult:
        _, resolved_username = self._resolve_email_otp_secret_username(username=username, require_email_otp_strategy=False)
        self._secret_store.delete_email_otp_password(username=resolved_username)
        return CommandResult(
            data={
                "ok": True,
                "username": resolved_username,
                "secret_storage": "macos_keychain",
                "auth_strategy": "email_otp",
            },
            source="bootstrap",
            capability="partial",
        )

    def _require_email_otp_config(
        self,
        *,
        base_url: str | None = None,
        dashboard_path: str | None = None,
        username: str | None = None,
    ) -> tuple[KlmsConfig, str]:
        existing = maybe_load_config(self._paths)
        resolved_username = str(username or "").strip() or (existing.auth_username if existing else None)
        config = save_config(
            self._paths,
            base_url=base_url,
            dashboard_path=dashboard_path,
            auth_username=resolved_username,
        )
        if config.auth_strategy != "email_otp":
            raise CommandError(
                code="AUTH_FLOW_UNSUPPORTED",
                message="KLMS auth is not configured for staged email OTP refresh.",
                hint="Run `kaist klms auth setup-email-otp --username <KAIST_ID>` first.",
                exit_code=10,
                retryable=False,
            )
        if not config.auth_username:
            raise CommandError(
                code="CONFIG_INVALID",
                message="KLMS email OTP auth requires a saved KAIST username.",
                hint="Run `kaist klms auth setup-email-otp --username <KAIST_ID>` first.",
                exit_code=40,
                retryable=False,
            )
        return config, config.auth_username

    def _auth_session_payload(
        self,
        *,
        config: KlmsConfig,
        username: str,
        challenge_url: str,
    ) -> dict[str, Any]:
        return {
            "session_id": new_auth_session_id(),
            "strategy": "email_otp",
            "stage": "starting",
            "username": username,
            "otp_source": config.otp_source or "manual",
            "base_url": config.base_url,
            "dashboard_path": config.dashboard_path,
            "challenge_url": challenge_url,
            "started_at": utc_now_iso(),
            "expires_at": session_expiry_iso(ttl_seconds=EMAIL_OTP_SESSION_TTL_SECONDS),
        }

    def _email_otp_worker_command(self, session_id: str) -> list[str]:
        if bool(getattr(sys, "frozen", False)):
            return [sys.executable, "klms", "auth", "_worker-run", session_id]
        return [sys.executable, "-m", "kaist_cli.main", "klms", "auth", "_worker-run", session_id]

    def _spawn_email_otp_worker(self, session_id: str) -> subprocess.Popen[str]:
        command = self._email_otp_worker_command(session_id)
        self._paths.auth_worker_log_path.parent.mkdir(parents=True, exist_ok=True)
        self._paths.auth_worker_log_path.write_text("", encoding="utf-8")
        log_handle = open(self._paths.auth_worker_log_path, "a", encoding="utf-8")
        try:
            return subprocess.Popen(  # noqa: S603
                command,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=log_handle,
                text=True,
                start_new_session=True,
                close_fds=True,
            )
        finally:
            log_handle.close()

    def _wait_for_email_otp_worker_ready(
        self,
        *,
        session_id: str,
        wait_seconds: float,
        worker: subprocess.Popen[str],
    ) -> dict[str, Any]:
        deadline = time.monotonic() + max(5.0, wait_seconds)
        while time.monotonic() < deadline:
            payload = self._require_active_auth_session(session_id)
            stage = str(payload.get("stage") or "").strip()
            if stage == "waiting_for_email_otp":
                return payload
            if stage == "completed":
                return payload
            if stage == "failed":
                error = payload.get("error")
                if isinstance(error, dict):
                    _raise_command_error_payload(error)
                raise CommandError(
                    code="AUTH_FAILED",
                    message="KLMS email OTP worker failed before reaching the OTP stage.",
                    hint="Retry `kaist klms auth begin-refresh`, or inspect the KAIST SSO flow manually.",
                    exit_code=10,
                    retryable=True,
                )
            if worker.poll() is not None:
                detail = self._read_auth_worker_log_tail(max_lines=12)
                detail_suffix = f" Last log lines:\n{detail}" if detail else ""
                clear_auth_session(self._paths)
                raise CommandError(
                    code="AUTH_FAILED",
                    message=f"KLMS email OTP worker exited before reaching the OTP stage.{detail_suffix}",
                    hint="Retry `kaist klms auth begin-refresh`, or inspect the KAIST SSO flow manually.",
                    exit_code=10,
                    retryable=True,
                )
            time.sleep(EMAIL_OTP_WORKER_POLL_SECONDS)
        raise CommandError(
            code="AUTH_TIMEOUT",
            message="Timed out waiting for the KLMS email OTP worker to reach the OTP stage.",
            hint="Retry `kaist klms auth begin-refresh`, or inspect the KAIST SSO flow manually.",
            exit_code=10,
            retryable=True,
        )

    def _send_email_otp_worker_command(
        self,
        *,
        payload: dict[str, Any],
        action: str,
        timeout_seconds: float,
        otp_code: str | None = None,
    ) -> dict[str, Any]:
        port = int(payload.get("worker_port") or 0)
        token = str(payload.get("worker_token") or "").strip()
        if port <= 0 or not token:
            raise CommandError(
                code="AUTH_SESSION_MISSING",
                message="Staged KLMS auth worker metadata was not found.",
                hint="Run `kaist klms auth begin-refresh` again to create a fresh staged session.",
                exit_code=10,
                retryable=True,
            )
        request_payload: dict[str, Any] = {"token": token, "action": action}
        if otp_code is not None:
            request_payload["otp"] = otp_code
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=max(1.0, timeout_seconds)) as sock:
                sock.settimeout(max(1.0, timeout_seconds))
                sock.sendall(json.dumps(request_payload).encode("utf-8"))
                sock.shutdown(socket.SHUT_WR)
                chunks: list[bytes] = []
                while True:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    chunks.append(chunk)
        except OSError as exc:
            worker_pid = int(payload.get("worker_pid") or 0)
            if worker_pid > 0 and not _pid_is_running(worker_pid):
                clear_auth_session(self._paths)
                raise CommandError(
                    code="AUTH_SESSION_EXPIRED",
                    message="The staged KLMS email OTP worker is no longer running.",
                    hint="Run `kaist klms auth begin-refresh` again to request a fresh OTP session.",
                    exit_code=10,
                    retryable=True,
                ) from exc
            raise CommandError(
                code="AUTH_FAILED",
                message=f"Could not reach the KLMS email OTP worker ({exc}).",
                hint="Run `kaist klms auth begin-refresh` again to create a fresh staged session.",
                exit_code=10,
                retryable=True,
            ) from exc
        try:
            response = json.loads(b"".join(chunks).decode("utf-8"))
        except Exception as exc:
            raise CommandError(
                code="AUTH_FAILED",
                message=f"KLMS email OTP worker returned an invalid response ({exc}).",
                hint="Run `kaist klms auth begin-refresh` again to create a fresh staged session.",
                exit_code=10,
                retryable=True,
            ) from exc
        if not isinstance(response, dict):
            raise CommandError(
                code="AUTH_FAILED",
                message="KLMS email OTP worker returned a non-object response.",
                hint="Run `kaist klms auth begin-refresh` again to create a fresh staged session.",
                exit_code=10,
                retryable=True,
            )
        return response

    def _persist_worker_failure(self, session_id: str, exc: CommandError) -> None:
        def updater(current: dict[str, Any]) -> dict[str, Any]:
            if str(current.get("session_id") or "").strip() != session_id:
                return current
            updated = dict(current)
            updated["stage"] = "failed"
            updated["error"] = _command_error_payload(exc)
            updated["finished_at"] = utc_now_iso()
            return updated

        try:
            from .auth_session import update_auth_session

            update_auth_session(self._paths, updater=updater)
        except Exception:
            pass

    def _require_active_auth_session(self, session_id: str) -> dict[str, Any]:
        payload = self._load_current_auth_session()
        if payload is None:
            raise CommandError(
                code="AUTH_SESSION_MISSING",
                message="No staged KLMS auth session was found.",
                hint="Run `kaist klms auth begin-refresh` first.",
                exit_code=10,
                retryable=True,
            )
        if str(payload.get("session_id") or "").strip() != str(session_id).strip():
            raise CommandError(
                code="AUTH_SESSION_MISSING",
                message=f"Staged KLMS auth session not found: {session_id}",
                hint="Run `kaist klms auth begin-refresh` again to create a fresh session.",
                exit_code=10,
                retryable=True,
            )
        stage = str(payload.get("stage") or "").strip()
        if stage in {"completed", "canceled"}:
            clear_auth_session(self._paths)
            raise CommandError(
                code="AUTH_SESSION_EXPIRED",
                message=f"Staged KLMS auth session is no longer active: {session_id}",
                hint="Run `kaist klms auth begin-refresh` again to request a fresh OTP challenge.",
                exit_code=10,
                retryable=True,
            )
        if stage == "failed":
            error = payload.get("error")
            if isinstance(error, dict):
                _raise_command_error_payload(error)
            clear_auth_session(self._paths)
            raise CommandError(
                code="AUTH_SESSION_EXPIRED",
                message=f"Staged KLMS auth session already failed: {session_id}",
                hint="Run `kaist klms auth begin-refresh` again to request a fresh OTP challenge.",
                exit_code=10,
                retryable=True,
            )
        return payload

    def _wait_for_email_otp_challenge(
        self,
        *,
        page: Any,
        context: Any,
        config: KlmsConfig,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + max(1.0, timeout_seconds)
        while time.monotonic() < deadline:
            current_url = _safe_page_url(page) or ""
            current_html = _safe_page_content(page) or ""
            if _page_has_authenticated_klms_session(page, config=config) or self._context_has_authenticated_page(context, config=config):
                self._persist_context_state(context)
                clear_auth_session(self._paths)
                return {"status": "completed"}
            if error_message := _extract_email_otp_error_message(current_html):
                raise CommandError(
                    code="AUTH_FAILED",
                    message=f"KAIST email OTP login failed before the OTP challenge ({error_message}).",
                    hint="Check the KAIST username/password, then rerun `kaist klms auth begin-refresh`.",
                    exit_code=10,
                    retryable=False,
                )
            if _looks_like_email_otp_page(current_url, current_html):
                return {"status": "waiting_for_email_otp", "challenge_url": current_url}
            page.wait_for_timeout(250)
        raise CommandError(
            code="AUTH_OTP_REQUIRED",
            message="Timed out waiting for the KAIST email OTP challenge page.",
            hint="Retry `kaist klms auth begin-refresh`; if KAIST changed the login flow, inspect it manually once.",
            exit_code=10,
            retryable=True,
        )

    def _wait_for_email_otp_completion(
        self,
        *,
        page: Any,
        context: Any,
        config: KlmsConfig,
        wait_seconds: float,
    ) -> None:
        deadline = time.monotonic() + max(1.0, wait_seconds)
        while time.monotonic() < deadline:
            if _page_has_authenticated_klms_session(page, config=config) or self._context_has_authenticated_page(context, config=config):
                self._persist_context_state(context)
                return
            current_html = _safe_page_content(page) or ""
            if error_message := _extract_email_otp_error_message(current_html):
                raise CommandError(
                    code="AUTH_OTP_INVALID",
                    message=f"KAIST rejected the email OTP code ({error_message}).",
                    hint="Check the newest KAIST email OTP and rerun `kaist klms auth complete-refresh <SESSION_ID> --otp CODE`.",
                    exit_code=10,
                    retryable=True,
                )
            state = self._context_dashboard_state(context, config=config, timeout_ms=5_000)
            if state["authenticated"]:
                self._persist_context_state(context)
                return
            page.wait_for_timeout(500)
        raise CommandError(
            code="AUTH_OTP_TIMEOUT",
            message="Timed out waiting for KLMS session completion after submitting the email OTP.",
            hint="Retry `kaist klms auth complete-refresh <SESSION_ID> --otp CODE`, or begin a fresh staged refresh.",
            exit_code=10,
            retryable=True,
        )

    def _validate_email_otp_request(self, *, context: Any, otp_code: str) -> str:
        response = context.request.post(
            "https://sso.kaist.ac.kr/auth/kaist/user/login/second/ajaxValidCrtfcNo",
            form={"crtfc_no": otp_code},
        )
        payload = _response_json_object(response)
        if payload is None:
            raise CommandError(
                code="AUTH_FAILED",
                message="KAIST returned a non-JSON response while validating the email OTP.",
                hint="Retry `kaist klms auth complete-refresh <SESSION_ID> --otp CODE`, or begin a fresh staged refresh.",
                exit_code=10,
                retryable=True,
            )
        code = str(payload.get("code") or "").strip()
        if code in {"SS0001", "SS0099"}:
            return code
        if code == "E001":
            raise CommandError(
                code="AUTH_OTP_INVALID",
                message="KAIST rejected the email OTP code (인증 번호가 올바르지않습니다.).",
                hint="Check the newest KAIST email OTP and rerun `kaist klms auth complete-refresh <SESSION_ID> --otp CODE`.",
                exit_code=10,
                retryable=True,
            )
        if code == "E002":
            raise CommandError(
                code="AUTH_OTP_TIMEOUT",
                message="The KAIST email OTP expired before it was submitted.",
                hint="Run `kaist klms auth begin-refresh` again to request a fresh OTP.",
                exit_code=10,
                retryable=True,
            )
        if code == "E003":
            raise CommandError(
                code="AUTH_FAILED",
                message="KAIST rejected the email OTP after too many attempts.",
                hint="Run `kaist klms auth begin-refresh` again to request a fresh OTP.",
                exit_code=10,
                retryable=True,
            )
        if code == "ES0017":
            raise CommandError(
                code="AUTH_SESSION_EXPIRED",
                message="KAIST no longer accepts this staged email OTP session.",
                hint="Run `kaist klms auth begin-refresh` again to request a fresh OTP.",
                exit_code=10,
                retryable=True,
            )
        detail = json.dumps(payload, ensure_ascii=False)
        raise CommandError(
            code="AUTH_FAILED",
            message=f"KAIST returned an unexpected email OTP validation response ({detail}).",
            hint="Retry `kaist klms auth complete-refresh <SESSION_ID> --otp CODE`, or begin a fresh staged refresh.",
            exit_code=10,
            retryable=True,
        )

    def _complete_email_otp_device_registration(
        self,
        *,
        page: Any,
        context: Any,
        config: KlmsConfig,
        wait_seconds: float,
    ) -> None:
        deadline = time.monotonic() + max(1.0, wait_seconds)
        triggered = False
        while time.monotonic() < deadline:
            if _page_has_authenticated_klms_session(page, config=config) or self._context_has_authenticated_page(context, config=config):
                self._persist_context_state(context)
                return
            current_url = (_safe_page_url(page) or "").lower()
            if "/auth/kaist/user/device/view" in current_url:
                if not triggered:
                    triggered = _complete_easy_login_device_registration(page)
                    if triggered:
                        page.wait_for_timeout(500)
                        continue
                if triggered:
                    try:
                        page.goto("https://sso.kaist.ac.kr/auth/kaist/user/device/login", wait_until="domcontentloaded", timeout=10_000)
                    except Exception:
                        page.wait_for_timeout(500)
                    continue
            state = self._context_dashboard_state(context, config=config, timeout_ms=5_000)
            if state["authenticated"]:
                self._persist_context_state(context)
                return
            page.wait_for_timeout(500)
        raise CommandError(
            code="AUTH_TIMEOUT",
            message="Timed out waiting for KAIST device registration to complete after email OTP validation.",
            hint="Retry `kaist klms auth complete-refresh <SESSION_ID> --otp CODE`, or begin a fresh staged refresh.",
            exit_code=10,
            retryable=True,
        )

    def _wait_for_email_otp_delivery(
        self,
        *,
        page: Any,
        timeout_seconds: float,
    ) -> None:
        deadline = time.monotonic() + max(1.0, timeout_seconds)
        while time.monotonic() < deadline:
            html = _safe_page_content(page) or ""
            lowered = html.lower()
            if "인증 번호가 발송되었습니다" in html or "verification code has been sent" in lowered:
                return
            try:
                status = page.evaluate(
                    """
                    () => {
                      const input = document.querySelector('#crtfc_no');
                      const submit = document.querySelector('#proc');
                      const result = document.querySelector('#resultMsg');
                      return {
                        inputReady: !!input && !input.disabled,
                        submitReady: !!submit && !submit.disabled,
                        resultText: (result?.textContent || '').trim(),
                      };
                    }
                    """
                ) or {}
            except Exception:
                status = {}
            if bool(status.get("inputReady")) and bool(status.get("submitReady")):
                return
            error_message = _extract_email_otp_error_message(html)
            if error_message and "발송" in error_message:
                raise CommandError(
                    code="AUTH_OTP_REQUIRED",
                    message=f"KAIST did not send the email OTP ({error_message}).",
                    hint="Retry `kaist klms auth begin-refresh`, or inspect the KAIST second-step page manually.",
                    exit_code=10,
                    retryable=True,
                )
            page.wait_for_timeout(250)
        raise CommandError(
            code="AUTH_OTP_REQUIRED",
            message="Timed out waiting for KAIST to send the email OTP.",
            hint="Retry `kaist klms auth begin-refresh`, or inspect the KAIST second-step page manually.",
            exit_code=10,
            retryable=True,
        )

    def _assert_saved_auth_session_reusable_with_playwright(
        self,
        *,
        playwright: Any,
        config: KlmsConfig,
        timeout_seconds: float,
    ) -> None:
        timeout_ms = max(1_000, int(max(1.0, timeout_seconds) * 1000))
        attempts: list[dict[str, Any]] = []

        if has_profile_session(self._paths):
            profile_context = _launch_chromium_persistent_context_sync(
                playwright,
                paths=self._paths,
                user_data_dir=str(self._paths.profile_dir),
                headless=True,
                accept_downloads=False,
            )
            try:
                state = self._context_dashboard_state(profile_context, config=config, timeout_ms=timeout_ms)
                attempts.append({"auth_mode": "profile", **state})
                if state["authenticated"]:
                    return
            finally:
                profile_context.close()

        if has_storage_state_session(self._paths):
            browser = _launch_chromium_browser_sync(playwright, paths=self._paths, headless=True)
            try:
                storage_context = browser.new_context(
                    storage_state=str(self._paths.storage_state_path),
                    accept_downloads=False,
                )
                try:
                    state = self._context_dashboard_state(storage_context, config=config, timeout_ms=timeout_ms)
                    attempts.append({"auth_mode": "storage_state", **state})
                    if state["authenticated"]:
                        return
                finally:
                    storage_context.close()
            finally:
                browser.close()

        detail = "; ".join(
            f"{attempt['auth_mode']}: final_url={attempt.get('final_url')}"
            for attempt in attempts
        ) or "no reusable auth artifacts"
        raise CommandError(
            code="AUTH_FAILED",
            message=f"KLMS login completed, but the saved session could not be reopened ({detail}).",
            hint="Run `kaist klms auth refresh` again; if it still fails, switch auth strategy or inspect `kaist klms auth doctor`.",
            exit_code=10,
            retryable=True,
        )

    def _assert_storage_state_reusable(
        self,
        *,
        browser: Any,
        config: KlmsConfig,
        timeout_seconds: float,
    ) -> None:
        if not has_storage_state_session(self._paths):
            raise CommandError(
                code="AUTH_FAILED",
                message="KLMS email OTP login completed, but no storage_state was written.",
                hint="Run `kaist klms auth begin-refresh` again to create a fresh staged session.",
                exit_code=10,
                retryable=True,
            )
        storage_context = browser.new_context(
            storage_state=str(self._paths.storage_state_path),
            accept_downloads=False,
        )
        try:
            state = self._context_dashboard_state(
                storage_context,
                config=config,
                timeout_ms=max(1_000, int(max(1.0, timeout_seconds) * 1000)),
            )
        finally:
            storage_context.close()
        if state["authenticated"]:
            return
        raise CommandError(
            code="AUTH_FAILED",
            message=f"KLMS email OTP login completed, but the saved session could not be reopened (storage_state: final_url={state.get('final_url')}).",
            hint="Run `kaist klms auth begin-refresh` again to request a fresh OTP session.",
            exit_code=10,
            retryable=True,
        )

    def begin_refresh(
        self,
        *,
        base_url: str | None = None,
        dashboard_path: str | None = None,
        username: str | None = None,
        wait_seconds: float = EMAIL_OTP_DEFAULT_WAIT_SECONDS,
    ) -> CommandResult:
        config, resolved_username = self._require_email_otp_config(
            base_url=base_url,
            dashboard_path=dashboard_path,
            username=username,
        )
        clear_auth_session(self._paths)
        payload = save_auth_session(
            self._paths,
            self._auth_session_payload(
                config=config,
                username=resolved_username,
                challenge_url=config.base_url,
            ),
        )
        worker = self._spawn_email_otp_worker(payload["session_id"])
        from .auth_session import update_auth_session

        update_auth_session(
            self._paths,
            updater=lambda current: {
                **current,
                "worker_pid": getattr(worker, "pid", 0) or None,
            }
            if str(current.get("session_id") or "").strip() == payload["session_id"]
            else current,
        )
        ready = self._wait_for_email_otp_worker_ready(
            session_id=payload["session_id"],
            wait_seconds=min(max(float(wait_seconds), 15.0), EMAIL_OTP_WORKER_READY_TIMEOUT_SECONDS),
            worker=worker,
        )
        stage = str(ready.get("stage") or "").strip()
        if stage == "completed":
            clear_auth_session(self._paths)
            return CommandResult(
                data={
                    "ok": True,
                    "state": "completed",
                    "login_strategy": "email_otp",
                    "username": resolved_username,
                },
                source="browser",
                capability="full",
            )
        return CommandResult(
            data={
                "ok": True,
                "state": "waiting_for_email_otp",
                "session_id": ready["session_id"],
                "strategy": ready["strategy"],
                "username": resolved_username,
                "otp_source": ready["otp_source"],
                "started_at": ready["started_at"],
                "expires_at": ready["expires_at"],
            },
            source="browser",
            capability="partial",
        )

    def complete_refresh(self, session_id: str, *, otp: str, wait_seconds: float = EMAIL_OTP_DEFAULT_WAIT_SECONDS) -> CommandResult:
        payload = self._require_active_auth_session(session_id)
        otp_code = re.sub(r"\D+", "", str(otp or "").strip())
        if not otp_code:
            raise CommandError(
                code="CONFIG_INVALID",
                message="OTP code must contain digits.",
                hint="Pass the code from the KAIST email with `--otp CODE`.",
                exit_code=40,
                retryable=False,
            )
        wait_seconds = max(15.0, min(float(wait_seconds), 600.0))
        response = self._send_email_otp_worker_command(
            payload=payload,
            action="submit_otp",
            otp_code=otp_code,
            timeout_seconds=wait_seconds,
        )
        if not bool(response.get("ok")):
            error = response.get("error")
            if isinstance(error, dict):
                _raise_command_error_payload(error)
            raise CommandError(
                code="AUTH_FAILED",
                message="KLMS email OTP worker returned an unexpected failure response.",
                hint="Run `kaist klms auth begin-refresh` again to create a fresh staged session.",
                exit_code=10,
                retryable=True,
            )
        data = response.get("data")
        if not isinstance(data, dict):
            raise CommandError(
                code="AUTH_FAILED",
                message="KLMS email OTP worker returned an invalid success response.",
                hint="Run `kaist klms auth begin-refresh` again to create a fresh staged session.",
                exit_code=10,
                retryable=True,
            )
        return CommandResult(data=data, source="browser", capability="full")

    def cancel_refresh(self, session_id: str) -> CommandResult:
        payload = self._require_active_auth_session(session_id)
        if payload.get("worker_port") and payload.get("worker_token"):
            response = self._send_email_otp_worker_command(
                payload=payload,
                action="cancel",
                timeout_seconds=5.0,
            )
            if bool(response.get("ok")) and isinstance(response.get("data"), dict):
                return CommandResult(data=response["data"], source="bootstrap", capability="partial")
        clear_auth_session(self._paths)
        return CommandResult(
            data={
                "ok": True,
                "state": "canceled",
                "session_id": str(payload.get("session_id") or "").strip() or session_id,
                "strategy": str(payload.get("strategy") or "").strip() or None,
            },
            source="bootstrap",
            capability="partial",
        )

    def worker_run(self, session_id: str) -> CommandResult:
        payload = self._require_active_auth_session(session_id)
        config = save_config(
            self._paths,
            base_url=str(payload.get("base_url") or "").strip() or None,
            dashboard_path=str(payload.get("dashboard_path") or "").strip() or None,
            auth_username=str(payload.get("username") or "").strip() or None,
        )
        username = str(payload.get("username") or "").strip()
        password = self._secret_store.load_email_otp_password(username=username)
        configure_playwright_env(self._paths)

        def mark_stage(**fields: Any) -> dict[str, Any]:
            from .auth_session import update_auth_session

            def updater(current: dict[str, Any]) -> dict[str, Any]:
                if str(current.get("session_id") or "").strip() != session_id:
                    return current
                updated = dict(current)
                updated.update(fields)
                return updated

            return update_auth_session(self._paths, updater=updater)

        from playwright.sync_api import sync_playwright  # type: ignore[import-untyped]

        try:
            with _hold_profile_lock(self._paths):
                with sync_playwright() as playwright:
                    browser = _launch_chromium_browser_sync(playwright, paths=self._paths, headless=True)
                    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    server.bind(("127.0.0.1", 0))
                    server.listen(1)
                    server.settimeout(0.5)
                    context: Any | None = None
                    try:
                        mark_stage(worker_pid=os.getpid())
                        context = browser.new_context(accept_downloads=False)
                        page = context.new_page()
                        page.goto(config.base_url, wait_until="domcontentloaded", timeout=30_000)
                        current_url = _safe_page_url(page) or ""
                        html = _safe_page_content(page) or ""
                        sso_url = _extract_sso_login_view_url(current_url, html) or current_url
                        page.goto(sso_url, wait_until="domcontentloaded", timeout=30_000)
                        if not _submit_password_login(page, username=username, password=password):
                            raise CommandError(
                                code="AUTH_FLOW_UNSUPPORTED",
                                message="Could not find the KAIST username/password login form for staged email OTP auth.",
                                hint="Use the manual browser login flow once, or update the staged email OTP selectors.",
                                exit_code=10,
                                retryable=False,
                            )
                        state = self._wait_for_email_otp_challenge(
                            page=page,
                            context=context,
                            config=config,
                            timeout_seconds=30.0,
                        )
                        if state["status"] == "completed":
                            clear_auth_session(self._paths)
                            return CommandResult(
                                data={"ok": True, "state": "completed", "login_strategy": "email_otp", "username": username},
                                source="browser",
                                capability="full",
                            )
                        if not _request_email_otp_delivery(page):
                            raise CommandError(
                                code="AUTH_FLOW_UNSUPPORTED",
                                message="Could not find the KAIST email OTP delivery control on the second-step page.",
                                hint="Inspect the KAIST second-step page manually, then update the staged email OTP selectors.",
                                exit_code=10,
                                retryable=False,
                            )
                        self._wait_for_email_otp_delivery(page=page, timeout_seconds=30.0)
                        token = token_hex(16)
                        port = int(server.getsockname()[1])
                        mark_stage(
                            stage="waiting_for_email_otp",
                            challenge_url=str(state.get("challenge_url") or _safe_page_url(page) or config.base_url),
                            worker_pid=os.getpid(),
                            worker_port=port,
                            worker_token=token,
                        )

                        def send_response(conn: socket.socket, payload_obj: dict[str, Any]) -> None:
                            conn.sendall(json.dumps(payload_obj).encode("utf-8"))

                        while True:
                            active = load_auth_session(self._paths)
                            if active is None or str(active.get("session_id") or "").strip() != session_id:
                                break
                            try:
                                conn, _ = server.accept()
                            except TimeoutError:
                                conn = None
                            except socket.timeout:
                                conn = None
                            if conn is None:
                                continue
                            with conn:
                                raw = b""
                                while True:
                                    chunk = conn.recv(4096)
                                    if not chunk:
                                        break
                                    raw += chunk
                                try:
                                    request_payload = json.loads(raw.decode("utf-8"))
                                except Exception:
                                    send_response(
                                        conn,
                                        {"ok": False, "error": _command_error_payload(CommandError(code="AUTH_FAILED", message="Invalid worker request payload.", exit_code=10, retryable=True))},
                                    )
                                    continue
                                if str(request_payload.get("token") or "").strip() != token:
                                    send_response(
                                        conn,
                                        {"ok": False, "error": _command_error_payload(CommandError(code="AUTH_FAILED", message="Unauthorized worker request.", exit_code=10, retryable=False))},
                                    )
                                    continue
                                action = str(request_payload.get("action") or "").strip()
                                if action == "cancel":
                                    clear_auth_session(self._paths)
                                    send_response(conn, {"ok": True, "data": {"ok": True, "state": "canceled", "session_id": session_id, "strategy": "email_otp"}})
                                    return CommandResult(data={"ok": True}, source="bootstrap", capability="partial")
                                if action != "submit_otp":
                                    send_response(
                                        conn,
                                        {"ok": False, "error": _command_error_payload(CommandError(code="AUTH_FAILED", message=f"Unsupported worker action: {action}", exit_code=10, retryable=False))},
                                    )
                                    continue
                                otp_code = re.sub(r"\D+", "", str(request_payload.get("otp") or "").strip())
                                try:
                                    validation_code = self._validate_email_otp_request(context=context, otp_code=otp_code)
                                    if validation_code == "SS0099":
                                        page.goto("https://sso.kaist.ac.kr/auth/kaist/user/device/view", wait_until="domcontentloaded", timeout=30_000)
                                        self._complete_email_otp_device_registration(
                                            page=page,
                                            context=context,
                                            config=config,
                                            wait_seconds=EMAIL_OTP_DEFAULT_WAIT_SECONDS,
                                        )
                                    else:
                                        page.goto("https://sso.kaist.ac.kr/auth/user/login/link", wait_until="domcontentloaded", timeout=30_000)
                                    self._wait_for_email_otp_completion(page=page, context=context, config=config, wait_seconds=EMAIL_OTP_DEFAULT_WAIT_SECONDS)
                                    self._assert_storage_state_reusable(
                                        browser=browser,
                                        config=config,
                                        timeout_seconds=15.0,
                                    )
                                    clear_auth_session(self._paths)
                                    send_response(
                                        conn,
                                        {
                                            "ok": True,
                                            "data": {
                                                "ok": True,
                                                "state": "completed",
                                                "login_strategy": "email_otp",
                                                "username": config.auth_username,
                                            },
                                        },
                                    )
                                    return CommandResult(data={"ok": True}, source="browser", capability="full")
                                except CommandError as exc:
                                    if exc.code not in {"AUTH_OTP_INVALID"}:
                                        self._persist_worker_failure(session_id, exc)
                                    send_response(conn, {"ok": False, "error": _command_error_payload(exc)})
                                    if exc.code not in {"AUTH_OTP_INVALID"}:
                                        return CommandResult(data={"ok": False}, source="browser", capability="partial")
                    finally:
                        try:
                            server.close()
                        except Exception:
                            pass
                        try:
                            if context is not None:
                                context.close()
                        finally:
                            browser.close()
        except CommandError as exc:
            self._persist_worker_failure(session_id, exc)
            return CommandResult(data={"ok": False}, source="browser", capability="partial")
        except Exception as exc:  # noqa: BLE001
            print(traceback.format_exc(), file=sys.stderr)
            wrapped = CommandError(
                code="AUTH_FAILED",
                message=f"Unexpected KLMS email OTP worker failure ({exc}).",
                hint="Retry `kaist klms auth begin-refresh`; if it still fails, inspect `kaist klms auth status` and the auth worker log.",
                exit_code=10,
                retryable=True,
            )
            self._persist_worker_failure(session_id, wrapped)
            return CommandResult(data={"ok": False}, source="browser", capability="partial")

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
                self._assert_saved_auth_session_reusable_with_playwright(
                    playwright=playwright,
                    config=config,
                    timeout_seconds=15.0,
                )

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
                    result = self._wait_for_easy_login_approval(
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
                self._assert_saved_auth_session_reusable_with_playwright(
                    playwright=playwright,
                    config=config,
                    timeout_seconds=15.0,
                )
                return result

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
        if existing is not None and existing.auth_strategy == "email_otp":
            return self.begin_refresh(
                base_url=base_url,
                dashboard_path=dashboard_path,
                username=resolved_username,
                wait_seconds=wait_seconds,
            )
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
