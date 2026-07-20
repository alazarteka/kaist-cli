from __future__ import annotations

import getpass
import json
import os
import re
import socket
import subprocess
import sys
import time
import traceback
from datetime import datetime
from secrets import token_hex
from typing import Any

from ..contracts import CommandError, CommandResult
from .auth_browser import (
    _hold_profile_lock,
    _launch_chromium_browser_sync,
)
from .auth_session import (
    clear_auth_session,
    load_auth_session,
    utc_now_iso,
)
from .auth_sso import (
    _complete_easy_login_device_registration,
    _extract_email_otp_error_message,
    _extract_sso_login_view_url,
    _looks_like_email_otp_page,
    _page_has_authenticated_klms_session,
    _request_email_otp_delivery,
    _response_json_object,
    _safe_page_content,
    _safe_page_url,
    _submit_password_login,
)
from .config import KlmsConfig, maybe_load_config, save_config
from .paths import configure_playwright_env

EMAIL_OTP_DEFAULT_WAIT_SECONDS = 180.0

EMAIL_OTP_SESSION_TTL_SECONDS = 10 * 60

EMAIL_OTP_WORKER_READY_TIMEOUT_SECONDS = 45.0

EMAIL_OTP_WORKER_POLL_SECONDS = 0.25

AUTH_SESSION_STARTING_GRACE_SECONDS = 30.0

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

class AuthOtpMixin:
    def _email_otp_secret_command(self, username: str | None) -> str:
        suffix = f" --username {username}" if username else ""
        return f"`kaist klms auth store-email-otp-secret{suffix}`"


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
        try:
            config = maybe_load_config(self._paths)
        except CommandError as exc:
            if exc.code != "CONFIG_INVALID":
                raise
            config = None
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
        try:
            existing = maybe_load_config(self._paths)
        except CommandError as exc:
            # Do not rewrite via save_config defaults (easy_login); email OTP
            # recovery must go through setup-email-otp so strategy stays correct.
            if exc.code != "CONFIG_INVALID":
                raise
            raise CommandError(
                code="CONFIG_INVALID",
                message=exc.message,
                hint="Run `kaist klms auth setup-email-otp --username <KAIST_ID>` to rewrite the invalid config.",
                exit_code=40,
                retryable=False,
            ) from exc
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

        from .auth_session import update_auth_session

        update_auth_session(self._paths, updater=updater)


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

