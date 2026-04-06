from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from ..contracts import CommandError
from .paths import KlmsPaths, ensure_private_dirs

AuthStrategy = Literal["easy_login", "email_otp"]


@dataclass(frozen=True)
class KlmsConfig:
    base_url: str
    dashboard_path: str
    auth_username: str | None
    auth_strategy: AuthStrategy
    otp_source: str | None
    course_ids: tuple[str, ...]
    notice_board_ids: tuple[str, ...]
    exclude_course_title_patterns: tuple[str, ...]


def _normalize_base_url(value: str) -> str:
    base_url = value.strip().rstrip("/")
    if not base_url:
        raise CommandError(
            code="CONFIG_INVALID",
            message="KLMS base URL is required.",
            hint="Pass --base-url https://klms.kaist.ac.kr or create a config file first.",
            exit_code=40,
        )
    if not base_url.startswith("http://") and not base_url.startswith("https://"):
        raise CommandError(
            code="CONFIG_INVALID",
            message=f"Invalid base URL: {value}",
            hint="Use a full URL such as https://klms.kaist.ac.kr.",
            exit_code=40,
        )
    return base_url


def _normalize_dashboard_path(value: str | None) -> str:
    dashboard_path = (value or "/my/").strip() or "/my/"
    if not dashboard_path.startswith("/"):
        dashboard_path = "/" + dashboard_path
    return dashboard_path


def _normalize_auth_username(value: str | None) -> str | None:
    username = (value or "").strip()
    return username or None


def _normalize_auth_strategy(value: str | None) -> AuthStrategy:
    text = str(value or "easy_login").strip().lower() or "easy_login"
    if text not in {"easy_login", "email_otp"}:
        raise CommandError(
            code="CONFIG_INVALID",
            message=f"Unsupported auth_strategy: {value}",
            hint="Use `easy_login` or `email_otp`.",
            exit_code=40,
        )
    return text  # type: ignore[return-value]


def _normalize_otp_source(value: str | None) -> str | None:
    text = str(value or "").strip()
    return text or None


def _coerce_list(raw: Any, *, field_name: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise CommandError(
            code="CONFIG_INVALID",
            message=f"{field_name} must be a list in {field_name}.",
            hint="Use TOML arrays such as course_ids = [\"177688\"].",
            exit_code=40,
        )
    return tuple(str(item).strip() for item in raw if str(item).strip())


def _toml_quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def load_config(paths: KlmsPaths) -> KlmsConfig:
    ensure_private_dirs(paths)
    if not paths.config_path.exists():
        raise CommandError(
            code="CONFIG_MISSING",
            message=f"KLMS config not found at {paths.config_path}.",
            hint="Run `kaist klms auth login --base-url https://klms.kaist.ac.kr` first.",
            exit_code=40,
        )

    import tomllib

    try:
        data = tomllib.loads(paths.config_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise CommandError(
            code="CONFIG_INVALID",
            message=f"Could not parse KLMS config at {paths.config_path}: {exc}",
            hint="Rewrite the config via `kaist klms auth login --base-url ...`.",
            exit_code=40,
        ) from exc

    return KlmsConfig(
        base_url=_normalize_base_url(str(data.get("base_url", ""))),
        dashboard_path=_normalize_dashboard_path(str(data.get("dashboard_path", "/my/"))),
        auth_username=_normalize_auth_username(data.get("auth_username")),
        auth_strategy=_normalize_auth_strategy(data.get("auth_strategy")),
        otp_source=_normalize_otp_source(data.get("otp_source")),
        course_ids=_coerce_list(data.get("course_ids"), field_name="course_ids"),
        notice_board_ids=_coerce_list(data.get("notice_board_ids"), field_name="notice_board_ids"),
        exclude_course_title_patterns=_coerce_list(
            data.get("exclude_course_title_patterns"),
            field_name="exclude_course_title_patterns",
        ),
    )


def maybe_load_config(paths: KlmsPaths) -> KlmsConfig | None:
    try:
        return load_config(paths)
    except CommandError:
        return None


def save_config(
    paths: KlmsPaths,
    *,
    base_url: str | None = None,
    dashboard_path: str | None = None,
    auth_username: str | None = None,
    auth_strategy: AuthStrategy | None = None,
    otp_source: str | None = None,
) -> KlmsConfig:
    ensure_private_dirs(paths)
    existing = maybe_load_config(paths)
    resolved_base_url = _normalize_base_url(base_url or (existing.base_url if existing else ""))
    resolved_dashboard_path = _normalize_dashboard_path(dashboard_path or (existing.dashboard_path if existing else "/my/"))
    resolved_auth_username = (
        _normalize_auth_username(auth_username)
        if auth_username is not None
        else (existing.auth_username if existing else None)
    )
    resolved_auth_strategy = (
        _normalize_auth_strategy(auth_strategy)
        if auth_strategy is not None
        else (existing.auth_strategy if existing else "easy_login")
    )
    resolved_otp_source = (
        _normalize_otp_source(otp_source)
        if otp_source is not None
        else (existing.otp_source if existing else None)
    )

    course_ids = existing.course_ids if existing else ()
    notice_board_ids = existing.notice_board_ids if existing else ()
    exclude_course_title_patterns = existing.exclude_course_title_patterns if existing else ()

    lines = [
        f"base_url = {_toml_quote(resolved_base_url)}",
        f"dashboard_path = {_toml_quote(resolved_dashboard_path)}",
        f"auth_username = {_toml_quote(resolved_auth_username or '')}",
        f"auth_strategy = {_toml_quote(resolved_auth_strategy)}",
        f"otp_source = {_toml_quote(resolved_otp_source or '')}",
        f"course_ids = {json.dumps(list(course_ids), ensure_ascii=False)}",
        f"notice_board_ids = {json.dumps(list(notice_board_ids), ensure_ascii=False)}",
        f"exclude_course_title_patterns = {json.dumps(list(exclude_course_title_patterns), ensure_ascii=False)}",
        "",
    ]
    paths.config_path.write_text("\n".join(lines), encoding="utf-8")

    return KlmsConfig(
        base_url=resolved_base_url,
        dashboard_path=resolved_dashboard_path,
        auth_username=resolved_auth_username,
        auth_strategy=resolved_auth_strategy,
        otp_source=resolved_otp_source,
        course_ids=course_ids,
        notice_board_ids=notice_board_ids,
        exclude_course_title_patterns=exclude_course_title_patterns,
    )


def abs_url(base_url: str, maybe_relative: str) -> str:
    if maybe_relative.startswith("http://") or maybe_relative.startswith("https://"):
        return maybe_relative
    path = maybe_relative if maybe_relative.startswith("/") else f"/{maybe_relative}"
    return base_url.rstrip("/") + path
