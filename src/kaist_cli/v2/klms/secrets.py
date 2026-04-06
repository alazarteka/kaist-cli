from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass

from ..contracts import CommandError

EMAIL_OTP_KEYCHAIN_SERVICE = "kaist-cli.klms.email-otp"


@dataclass(frozen=True)
class KeychainSecretStore:
    service: str = EMAIL_OTP_KEYCHAIN_SERVICE

    def _require_macos(self) -> None:
        if sys.platform != "darwin":
            raise CommandError(
                code="AUTH_FLOW_UNSUPPORTED",
                message="KLMS email OTP secret storage is currently supported only on macOS.",
                hint="Use the existing Easy Login flow, or add another secure secret backend first.",
                exit_code=10,
                retryable=False,
            )

    def store_email_otp_password(self, *, username: str, password: str) -> None:
        self._require_macos()
        completed = subprocess.run(  # noqa: S603
            [
                "security",
                "add-generic-password",
                "-U",
                "-s",
                self.service,
                "-a",
                username,
                "-w",
                password,
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or f"exit code {completed.returncode}").strip()
            raise CommandError(
                code="AUTH_SECRET_UNAVAILABLE",
                message=f"Could not store the KAIST password in macOS Keychain ({detail}).",
                hint="Check Keychain access permissions, then rerun `kaist klms auth store-email-otp-secret --username <KAIST_ID>`.",
                exit_code=10,
                retryable=True,
            )

    def load_email_otp_password(self, *, username: str) -> str:
        self._require_macos()
        completed = subprocess.run(  # noqa: S603
            [
                "security",
                "find-generic-password",
                "-s",
                self.service,
                "-a",
                username,
                "-w",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or f"exit code {completed.returncode}").strip()
            raise CommandError(
                code="AUTH_SECRET_UNAVAILABLE",
                message=f"KAIST password is not available from macOS Keychain ({detail}).",
                hint="Run `kaist klms auth store-email-otp-secret --username <KAIST_ID>` in a separate terminal to store the password.",
                exit_code=10,
                retryable=True,
            )
        password = str(completed.stdout or "").rstrip("\r\n")
        if not password:
            raise CommandError(
                code="AUTH_SECRET_UNAVAILABLE",
                message="KAIST password was blank when read from macOS Keychain.",
                hint="Run `kaist klms auth store-email-otp-secret --username <KAIST_ID>` again to reset the password.",
                exit_code=10,
                retryable=True,
            )
        return password

    def delete_email_otp_password(self, *, username: str) -> None:
        self._require_macos()
        completed = subprocess.run(  # noqa: S603
            [
                "security",
                "delete-generic-password",
                "-s",
                self.service,
                "-a",
                username,
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or f"exit code {completed.returncode}").strip()
            raise CommandError(
                code="AUTH_SECRET_UNAVAILABLE",
                message=f"Could not delete the KAIST password from macOS Keychain ({detail}).",
                hint="Check Keychain access permissions and retry.",
                exit_code=10,
                retryable=True,
            )
