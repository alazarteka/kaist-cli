from __future__ import annotations

import asyncio
from dataclasses import dataclass


@dataclass(frozen=True)
class CliErrorDescriptor:
    code: str
    exit_code: int
    retryable: bool
    hint: str | None


def classify_error(exc: Exception) -> CliErrorDescriptor:
    msg = str(exc)
    msg_l = msg.lower()
    name = exc.__class__.__name__

    code = getattr(exc, "code", None)
    exit_code = getattr(exc, "exit_code", None)
    retryable = getattr(exc, "retryable", None)
    hint = getattr(exc, "hint", None)
    if isinstance(code, str) and isinstance(exit_code, int) and isinstance(retryable, bool):
        return CliErrorDescriptor(code, exit_code, retryable, hint if isinstance(hint, str) or hint is None else str(hint))

    if name == "KlmsAuthError":
        return CliErrorDescriptor("AUTH_EXPIRED", 10, True, "kaist klms auth login")

    if name == "SelfUpdateError":
        return CliErrorDescriptor("UPDATE_FAILED", 60, False, "kaist update --check")

    if isinstance(exc, FileNotFoundError):
        if "config" in msg_l:
            return CliErrorDescriptor("CONFIG_INVALID", 40, False, "kaist klms auth login --base-url https://klms.kaist.ac.kr")
        if "login state" in msg_l or "storage state" in msg_l or "profile" in msg_l:
            return CliErrorDescriptor("AUTH_MISSING", 10, True, "kaist klms auth login")
        return CliErrorDescriptor("NOT_FOUND", 50, False, None)

    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return CliErrorDescriptor("NETWORK_TIMEOUT", 20, True, "retry the command")

    if isinstance(exc, ConnectionError):
        return CliErrorDescriptor("NETWORK_UNAVAILABLE", 20, True, "check network and retry")

    if isinstance(exc, ValueError):
        if any(token in msg_l for token in ["base_url", "config", "must be a list", "dashboard_path"]):
            return CliErrorDescriptor("CONFIG_INVALID", 40, False, "kaist klms auth login --base-url https://klms.kaist.ac.kr")
        if any(token in msg_l for token in ["response shape", "payload", "ajax"]):
            return CliErrorDescriptor("API_SHAPE_CHANGED", 30, True, "retry or run kaist klms dev probe --live")
        if any(token in msg_l for token in ["parse", "extract", "selector"]):
            return CliErrorDescriptor("PARSE_DRIFT", 30, True, "retry or run kaist klms dev discover")

    if any(token in msg_l for token in ["ssologin", "re-authenticate", "login state not found", "notloggedin"]):
        return CliErrorDescriptor("AUTH_EXPIRED", 10, True, "kaist klms auth login")

    if any(token in msg_l for token in ["timeout", "timed out"]):
        return CliErrorDescriptor("NETWORK_TIMEOUT", 20, True, "retry the command")

    if isinstance(exc, NotImplementedError):
        return CliErrorDescriptor("NOT_FOUND", 50, False, None)

    return CliErrorDescriptor("INTERNAL", 50, False, None)
