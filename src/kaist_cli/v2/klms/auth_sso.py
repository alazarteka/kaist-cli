from __future__ import annotations

import html as _html
import json
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup  # type: ignore[import-untyped]

from ..contracts import CommandError, CommandResult
from .auth_browser import (
    _hold_profile_lock,
    _launch_chromium_persistent_context_sync,
)
from .config import KlmsConfig
from .paths import KlmsPaths
from .browser_types import BrowserContextLike, PageLike

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

AUTH_SESSION_COOKIE_NAME = "moodlesession"

def epoch_to_iso_utc(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

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

def _safe_page_content(page: PageLike) -> str | None:
    try:
        return str(page.content() or "")
    except Exception:
        return None

def _safe_page_url(page: PageLike) -> str | None:
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

def _submit_password_login(page: PageLike, *, username: str, password: str) -> bool:
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

def _submit_email_otp_code(page: PageLike, *, otp: str) -> bool:
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

def _request_email_otp_delivery(page: PageLike) -> bool:
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

def _submit_easy_login_link(page: PageLike) -> bool:
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

def _complete_easy_login_device_registration(page: PageLike) -> bool:
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

def _page_has_authenticated_klms_session(page: PageLike, *, config: KlmsConfig) -> bool:
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
        return False
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
    valid_cookies = [cookie for cookie in cookies if isinstance(cookie, dict)]
    auth_cookies = [
        cookie
        for cookie in valid_cookies
        if str(cookie.get("name") or "").strip().lower() == AUTH_SESSION_COOKIE_NAME
    ]

    def expiry_epochs(items: list[dict[str, Any]]) -> list[float]:
        return [
            float(cookie["expires"])
            for cookie in items
            if isinstance(cookie.get("expires"), (int, float)) and float(cookie["expires"]) > 0
        ]

    exp_epochs = expiry_epochs(valid_cookies)
    auth_exp_epochs = expiry_epochs(auth_cookies)

    def expiry_payload(values: list[float]) -> dict[str, Any]:
        if not values:
            return {
                "expiring_cookie_count": 0,
                "next_expiry_iso": None,
                "next_expiry_in_hours": None,
                "latest_expiry_iso": None,
            }
        next_expiry = min(values)
        latest_expiry = max(values)
        return {
            "expiring_cookie_count": len(values),
            "next_expiry_iso": epoch_to_iso_utc(next_expiry),
            "next_expiry_in_hours": round((next_expiry - now_epoch) / 3600, 2),
            "latest_expiry_iso": epoch_to_iso_utc(latest_expiry),
        }

    return {
        "cookie_count": len(cookies),
        **expiry_payload(exp_epochs),
        "auth_cookie_count": len(auth_cookies),
        **{
            f"auth_{key}": value
            for key, value in expiry_payload(auth_exp_epochs).items()
        },
    }


class AuthEasyLoginMixin:
    def _wait_for_easy_login_init(self, page: PageLike, *, timeout_seconds: float) -> str:
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


    def _wait_for_easy_login_approval(
        self,
        *,
        page: PageLike,
        context: BrowserContextLike,
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

