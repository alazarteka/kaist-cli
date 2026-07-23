from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import time
from contextlib import nullcontext
from datetime import datetime, timezone
from typing import Any, Literal

from ..contracts import CommandError, CommandResult
from .auth_browser import (
    _concurrent_profile_access_error,
    _hold_profile_lock,
    _is_missing_browser_error,
    _launch_chromium_browser_sync,
    _launch_chromium_persistent_context_sync,
    _playwright_install_cmd,
    _resolve_system_chromium_executable,
    _system_browser_channel_candidates,
    _system_chromium_executable_candidates,
    _tail_text,
    install_browser,
)
from .auth_otp import (
    AUTH_SESSION_STARTING_GRACE_SECONDS,
    AuthOtpMixin,
    EMAIL_OTP_DEFAULT_WAIT_SECONDS,
    EMAIL_OTP_SESSION_TTL_SECONDS,
    EMAIL_OTP_WORKER_POLL_SECONDS,
    EMAIL_OTP_WORKER_READY_TIMEOUT_SECONDS,
    _command_error_payload,
    _parse_iso_utc,
    _pid_is_running,
    _raise_command_error_payload,
)
from .auth_session import (
    clear_auth_session,
    load_auth_session,
    new_auth_session_id,
    save_auth_session,
    session_expiry_iso,
    utc_now_iso,
)
from .auth_sso import (
    APP_LOGIN_PATHS,
    AuthEasyLoginMixin,
    AUTH_SESSION_COOKIE_NAME,
    EASY_LOGIN_DEFAULT_WAIT_SECONDS,
    EASY_LOGIN_POLL_SECONDS,
    EASY_LOGIN_SUBMIT_NEEDLE,
    EASY_LOGIN_VIEW_NEEDLE,
    HTML_LOGIN_MARKERS,
    URL_LOGIN_NEEDLES,
    _EasyLoginSignals,
    _complete_easy_login_device_registration,
    _evaluate_easy_login_mfa_payload,
    _evaluate_easy_login_policy_payload,
    _extract_easy_login_error_message,
    _extract_easy_login_number,
    _extract_email_otp_error_message,
    _extract_sso_login_view_url,
    _looks_like_easy_login_page,
    _looks_like_easy_login_verification_page,
    _looks_like_email_otp_page,
    _observe_easy_login_response,
    _page_has_authenticated_klms_session,
    _request_email_otp_delivery,
    _response_json_object,
    _response_json_payload,
    _safe_page_content,
    _safe_page_url,
    _should_update_easy_login_number,
    _submit_easy_login_link,
    _submit_email_otp_code,
    _submit_password_login,
    epoch_to_iso_utc,
    extract_sesskey,
    looks_logged_out_html,
    looks_login_url,
    storage_state_cookie_stats,
)
from .config import KlmsConfig, maybe_load_config, save_config
from .paths import KlmsPaths, chmod_best_effort, configure_playwright_env, ensure_private_dirs
from .secrets import KeychainSecretStore, SecretStore
from .browser_types import BrowserContextLike

AuthMode = Literal["profile", "storage_state", "none"]

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

def record_auth_verified(paths: KlmsPaths) -> None:
    """Write last_verified_at after a live dashboard check confirms an authenticated session."""
    ensure_private_dirs(paths)
    try:
        paths.auth_verified_path.write_text(
            json.dumps({"verified_at": utc_now_iso()}), encoding="utf-8"
        )
    except OSError:
        pass

def load_auth_verified(paths: KlmsPaths) -> str | None:
    try:
        raw = json.loads(paths.auth_verified_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = str(raw.get("verified_at") or "").strip() if isinstance(raw, dict) else ""
    return value or None

class AuthService(AuthOtpMixin, AuthEasyLoginMixin):
    def __init__(self, paths: KlmsPaths, *, secret_store: SecretStore | None = None) -> None:
        self._paths = paths
        self._secret_store = secret_store or KeychainSecretStore()

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

    @staticmethod
    def _session_expiry(cookie_stats: dict[str, Any] | None) -> dict[str, Any]:
        """Report MoodleSession cookie expiry; unknown when the cookie has no absolute expiry."""
        if not isinstance(cookie_stats, dict) or "read_error" in cookie_stats:
            return {
                "source": "unknown",
                "note": "No readable session cookies; session validity is confirmed at use.",
            }
        next_iso = cookie_stats.get("auth_next_expiry_iso")
        hours = cookie_stats.get("auth_next_expiry_in_hours")
        if next_iso is None or hours is None:
            return {
                "source": "unknown",
                "note": "The KLMS session cookie carries no expiry; session validity is confirmed at use.",
            }
        return {
            "source": "cookie_expiry",
            "next_expiry_iso": next_iso,
            "hours_until_expiry": hours,
            "latest_expiry_iso": cookie_stats.get("auth_latest_expiry_iso"),
            "overdue": hours <= 0,
        }

    def snapshot(self) -> dict[str, Any]:
        ensure_private_dirs(self._paths)
        config_error: dict[str, Any] | None = None
        try:
            config = maybe_load_config(self._paths)
        except CommandError as exc:
            # Status/probe paths must surface invalid config without raising so
            # callers can recommend `auth login` to rewrite the file.
            if exc.code != "CONFIG_INVALID":
                raise
            config = None
            config_error = {
                "code": exc.code,
                "message": exc.message,
                "hint": exc.hint,
            }
        mode = active_auth_mode(self._paths)
        staged_auth_session = self._session_snapshot()
        cookie_stats = storage_state_cookie_stats(self._paths)
        recommended = self._recommended_action(
            config=config,
            mode=mode,
            staged_auth_session=staged_auth_session,
        )
        if config_error is not None:
            recommended = (
                "Run `kaist klms auth login --base-url https://klms.kaist.ac.kr` "
                "to rewrite the invalid config."
            )
        payload: dict[str, Any] = {
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
            "session_expiry": self._session_expiry(cookie_stats),
            "last_verified_at": load_auth_verified(self._paths),
            "storage_state_cookie_stats": cookie_stats,
            "staged_auth_session": staged_auth_session,
            "config": self._config_payload(config),
            "validation_mode": "offline-only",
            "login_detection": {
                "html_markers": list(HTML_LOGIN_MARKERS),
                "url_needles": list(URL_LOGIN_NEEDLES),
                "app_login_paths": list(APP_LOGIN_PATHS),
            },
            "recommended_action": recommended,
        }
        if config_error is not None:
            payload["config_error"] = config_error
        return payload

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

    def status(self, *, verify: bool = False) -> CommandResult:
        snapshot = self.snapshot()
        if verify:
            live_check = self._live_check()
            snapshot["live_check"] = live_check
            if live_check.get("authenticated") is True:
                snapshot["last_verified_at"] = load_auth_verified(self._paths)
        return CommandResult(data=snapshot, source="bootstrap", capability="partial")

    def _live_check(self, *, timeout_seconds: float = 20.0) -> dict[str, Any]:
        """Live dashboard check for ``auth status --verify``; soft-fails to the caller."""
        try:
            config = maybe_load_config(self._paths)
        except CommandError as exc:
            note = (
                "KLMS config is invalid; run `kaist klms auth login` to rewrite it."
                if exc.code == "CONFIG_INVALID"
                else None
            )
            payload: dict[str, Any] = {
                "authenticated": None,
                "code": exc.code,
                "detail": exc.message,
                "checked_at": utc_now_iso(),
            }
            if note is not None:
                payload["note"] = note
            if exc.retryable:
                payload["retryable"] = True
            return payload
        if config is None:
            return {
                "authenticated": None,
                "note": "KLMS is not configured; run `kaist klms auth login` first.",
                "checked_at": utc_now_iso(),
            }
        try:
            result = self.run_authenticated_with_state(
                config=config,
                headless=True,
                accept_downloads=False,
                timeout_seconds=timeout_seconds,
                callback=lambda context, auth_mode, state: {
                    "authenticated": True,
                    "auth_mode": auth_mode,
                    "final_url": state.get("final_url"),
                },
            )
        except CommandError as exc:
            if exc.code in {"AUTH_EXPIRED", "AUTH_MISSING"}:
                return {
                    "authenticated": False,
                    "code": exc.code,
                    "detail": exc.message,
                    "checked_at": utc_now_iso(),
                }
            return {
                "authenticated": None,
                "code": exc.code,
                "detail": exc.message,
                "retryable": exc.retryable,
                "checked_at": utc_now_iso(),
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "authenticated": None,
                "code": "AUTH_CHECK_UNAVAILABLE",
                "detail": f"{type(exc).__name__}: {exc}",
                "retryable": True,
                "checked_at": utc_now_iso(),
            }
        result["checked_at"] = utc_now_iso()
        return result

    def install_browser(self, *, force: bool = False) -> CommandResult:
        return CommandResult(data=install_browser(self._paths, force=force), source="bootstrap", capability="partial")

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
                    record_auth_verified(self._paths)
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
                        record_auth_verified(self._paths)
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
            record_auth_verified(self._paths)
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

    def _persist_context_state(self, context: BrowserContextLike) -> None:
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

    def _context_has_authenticated_page(self, context: BrowserContextLike, *, config: KlmsConfig) -> bool:
        return any(_page_has_authenticated_klms_session(page, config=config) for page in context.pages)

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
        try:
            existing = maybe_load_config(self._paths)
        except CommandError as exc:
            # Corrupt config should fall through to login, which rewrites via save_config.
            if exc.code != "CONFIG_INVALID":
                raise
            existing = None
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

    def _context_dashboard_state(self, context: BrowserContextLike, *, config: KlmsConfig, timeout_ms: int) -> dict[str, Any]:
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
                        except Exception as exc:  # noqa: BLE001
                            attempts.append({"auth_mode": "profile", "check_error": str(exc)})
                        else:
                            attempts.append({"auth_mode": "profile", **state})
                            if state["authenticated"]:
                                record_auth_verified(self._paths)
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
                        except Exception as exc:  # noqa: BLE001
                            attempts.append({"auth_mode": "storage_state", "check_error": str(exc)})
                        else:
                            attempts.append({"auth_mode": "storage_state", **state})
                            if state["authenticated"]:
                                record_auth_verified(self._paths)
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
                else f"{attempt['auth_mode']}: check_error={attempt.get('check_error')}"
                if attempt.get("check_error")
                else f"{attempt['auth_mode']}: final_url={attempt.get('final_url')}"
            )
            for attempt in attempts
        ]
        detail = "; ".join(attempt_summaries) if attempt_summaries else "no attempts"
        if attempts and all("launch_error" in attempt or "check_error" in attempt for attempt in attempts):
            raise CommandError(
                code="AUTH_CHECK_UNAVAILABLE",
                message=f"Could not complete a live KLMS auth check ({detail}).",
                hint="Retry the command after checking network access and browser availability.",
                exit_code=10,
                retryable=True,
            )
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

        def probe_context(context: BrowserContextLike, *, auth_mode: str) -> dict[str, Any]:
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


# Public / test re-exports (symbols imported above are already in this module's namespace).
__all__ = [
    "APP_LOGIN_PATHS",
    "AUTH_SESSION_COOKIE_NAME",
    "AUTH_SESSION_STARTING_GRACE_SECONDS",
    "AuthMode",
    "AuthService",
    "EASY_LOGIN_DEFAULT_WAIT_SECONDS",
    "EASY_LOGIN_POLL_SECONDS",
    "EASY_LOGIN_SUBMIT_NEEDLE",
    "EASY_LOGIN_VIEW_NEEDLE",
    "EMAIL_OTP_DEFAULT_WAIT_SECONDS",
    "EMAIL_OTP_SESSION_TTL_SECONDS",
    "EMAIL_OTP_WORKER_POLL_SECONDS",
    "EMAIL_OTP_WORKER_READY_TIMEOUT_SECONDS",
    "HTML_LOGIN_MARKERS",
    "URL_LOGIN_NEEDLES",
    "_EasyLoginSignals",
    "_command_error_payload",
    "_complete_easy_login_device_registration",
    "_concurrent_profile_access_error",
    "_evaluate_easy_login_mfa_payload",
    "_evaluate_easy_login_policy_payload",
    "_extract_easy_login_error_message",
    "_extract_easy_login_number",
    "_extract_email_otp_error_message",
    "_extract_sso_login_view_url",
    "_hold_profile_lock",
    "_is_missing_browser_error",
    "_launch_chromium_browser_sync",
    "_launch_chromium_persistent_context_sync",
    "_looks_like_easy_login_page",
    "_looks_like_easy_login_verification_page",
    "_looks_like_email_otp_page",
    "_observe_easy_login_response",
    "_page_has_authenticated_klms_session",
    "_parse_iso_utc",
    "_pid_is_running",
    "_playwright_install_cmd",
    "_raise_command_error_payload",
    "_request_email_otp_delivery",
    "_resolve_system_chromium_executable",
    "_response_json_object",
    "_response_json_payload",
    "_safe_page_content",
    "_safe_page_url",
    "_should_update_easy_login_number",
    "_submit_easy_login_link",
    "_submit_email_otp_code",
    "_submit_password_login",
    "_system_browser_channel_candidates",
    "_system_chromium_executable_candidates",
    "_tail_text",
    "active_auth_mode",
    "epoch_to_iso_utc",
    "extract_sesskey",
    "has_profile_session",
    "has_storage_state_session",
    "install_browser",
    "load_auth_verified",
    "looks_logged_out_html",
    "looks_login_url",
    "record_auth_verified",
    "storage_state_cookie_stats",
]
