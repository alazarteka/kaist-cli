#!/usr/bin/env python3
"""
KAIST KLMS (read-only) implementation for CLI usage.

This module is intentionally conservative:
- Read-only access (no submissions, no posting, no mutations)
- Uses persisted Playwright auth artifacts under <KAIST_CLI_HOME>/private/klms/
- Designed for "sync + diff" workflows (assignments, notices, files)

NOTE: KLMS HTML structure varies and may change. This implementation provides a
scaffold and requires configuration + a one-time login bootstrap.
"""

from __future__ import annotations

import asyncio
import contextvars
import os
import re
import time
import subprocess
import sys
import html as _html
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Literal
from urllib.parse import parse_qs, quote, unquote, urlparse

from bs4 import BeautifulSoup  # type: ignore[import-untyped]

from .storage import read_json_file, update_json_file, write_json_file_atomic


def _kaist_cli_home() -> Path:
    return Path(
        os.environ.get("KAIST_CLI_HOME")
        or str(Path.home() / ".kaist-cli")
    ).expanduser()


PRIVATE_ROOT = _kaist_cli_home() / "private" / "klms"
DOWNLOAD_ROOT = _kaist_cli_home() / "files" / "klms"
PROFILE_DIR = PRIVATE_ROOT / "profile"
CONFIG_PATH = PRIVATE_ROOT / "config.toml"
STORAGE_STATE_PATH = PRIVATE_ROOT / "storage_state.json"
SNAPSHOT_PATH = PRIVATE_ROOT / "snapshot.json"
CACHE_PATH = PRIVATE_ROOT / "cache.json"
ENDPOINT_DISCOVERY_PATH = PRIVATE_ROOT / "endpoint_discovery.json"
API_MAP_PATH = PRIVATE_ROOT / "api_map.json"
PLAYWRIGHT_BROWSERS_DIR = PRIVATE_ROOT / "playwright-browsers"

_RUNTIME_CONTEXT: contextvars.ContextVar[Any | None] = contextvars.ContextVar(
    "klms_runtime_context",
    default=None,
)
_RUNTIME_AUTH_MODE: contextvars.ContextVar[Literal["profile", "storage_state"] | None] = contextvars.ContextVar(
    "klms_runtime_auth_mode",
    default=None,
)


def _ensure_private_dirs() -> None:
    PRIVATE_ROOT.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(PRIVATE_ROOT, 0o700)
    except PermissionError:
        pass
    DOWNLOAD_ROOT.mkdir(parents=True, exist_ok=True)


def _configure_playwright_env() -> Path:
    _ensure_private_dirs()
    PLAYWRIGHT_BROWSERS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(PLAYWRIGHT_BROWSERS_DIR, 0o700)
    except PermissionError:
        pass
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(PLAYWRIGHT_BROWSERS_DIR))
    return Path(os.environ["PLAYWRIGHT_BROWSERS_PATH"]).expanduser()


def _is_missing_browser_error(exc: BaseException) -> bool:
    message = str(exc).lower()
    return (
        "executable doesn't exist" in message
        or "download new browsers" in message
        or "playwright install" in message
    )


def _playwright_install_cmd() -> tuple[list[str], dict[str, str]]:
    _configure_playwright_env()
    from playwright._impl._driver import compute_driver_executable, get_driver_env  # type: ignore[import-untyped]

    node_path, cli_path = compute_driver_executable()
    env = os.environ.copy()
    env.update(get_driver_env())
    env["PLAYWRIGHT_BROWSERS_PATH"] = os.environ["PLAYWRIGHT_BROWSERS_PATH"]
    return [node_path, cli_path], env


def _tail_text(text: str, *, max_lines: int = 20) -> str:
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return ""
    return "\n".join(lines[-max_lines:])


def klms_install_browser(*, force: bool = False) -> dict[str, Any]:
    browser_path = _configure_playwright_env()
    driver_cmd, env = _playwright_install_cmd()
    cmd = [*driver_cmd, "install"]
    if force:
        cmd.append("--force")
    cmd.append("chromium")
    completed = subprocess.run(  # noqa: S603
        cmd,
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
        "command": cmd,
    }
    stdout_tail = _tail_text(completed.stdout)
    stderr_tail = _tail_text(completed.stderr)
    if stdout_tail:
        result["stdout_tail"] = stdout_tail
    if stderr_tail:
        result["stderr_tail"] = stderr_tail
    if completed.returncode != 0:
        hint = "Run `kaist klms auth install-browser --force` to retry."
        detail = stderr_tail or stdout_tail or f"exit code {completed.returncode}"
        raise RuntimeError(f"Failed to install Playwright Chromium ({detail}). {hint}")
    return result


def _read_positive_int_env(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(minimum, min(maximum, value))


def _concurrency_limit() -> int:
    return _read_positive_int_env("KAIST_KLMS_CONCURRENCY", default=4, minimum=1, maximum=16)


def _course_info_cache_ttl_seconds() -> int:
    return _read_positive_int_env("KAIST_KLMS_COURSE_INFO_TTL_SECONDS", default=6 * 3600, minimum=0, maximum=7 * 24 * 3600)


def _notice_board_cache_ttl_seconds() -> int:
    return _read_positive_int_env("KAIST_KLMS_NOTICE_BOARD_TTL_SECONDS", default=1800, minimum=0, maximum=7 * 24 * 3600)


def _cache_default() -> dict[str, Any]:
    return {"version": 1, "course_info": {}, "notice_board_discovery": {}}


def _load_cache() -> dict[str, Any]:
    _ensure_private_dirs()
    data = read_json_file(CACHE_PATH, default=_cache_default())
    if not isinstance(data, dict):
        return _cache_default()
    data.setdefault("version", 1)
    data.setdefault("course_info", {})
    data.setdefault("notice_board_discovery", {})
    return data


def _cache_get_course_info(course_id: str) -> dict[str, Any] | None:
    ttl = _course_info_cache_ttl_seconds()
    if ttl == 0:
        return None
    cache = _load_cache()
    entry = (cache.get("course_info") or {}).get(str(course_id))
    if not isinstance(entry, dict):
        return None
    fetched_at = entry.get("fetched_at_epoch")
    if not isinstance(fetched_at, (int, float)):
        return None
    if time.time() - float(fetched_at) > ttl:
        return None
    data = entry.get("data")
    return data if isinstance(data, dict) else None


def _cache_set_course_info(course_id: str, data: dict[str, Any]) -> None:
    def updater(cache: dict[str, Any]) -> dict[str, Any]:
        cache.setdefault("version", 1)
        course_info = cache.setdefault("course_info", {})
        if not isinstance(course_info, dict):
            course_info = {}
            cache["course_info"] = course_info
        course_info[str(course_id)] = {
            "fetched_at_epoch": time.time(),
            "fetched_at_iso": _utc_now_iso(),
            "data": data,
        }
        cache.setdefault("notice_board_discovery", {})
        return cache

    update_json_file(
        CACHE_PATH,
        default=_cache_default(),
        updater=updater,
        chmod_mode=0o600,
    )


def _course_ids_cache_key(course_ids: list[str]) -> str:
    normalized = sorted({str(c).strip() for c in course_ids if str(c).strip()})
    return ",".join(normalized)


def _cache_get_notice_board_ids(course_ids: list[str]) -> list[str] | None:
    ttl = _notice_board_cache_ttl_seconds()
    if ttl == 0:
        return None
    key = _course_ids_cache_key(course_ids)
    if not key:
        return None
    cache = _load_cache()
    discovery = cache.get("notice_board_discovery") or {}
    if not isinstance(discovery, dict):
        return None
    entry = discovery.get(key)
    if not isinstance(entry, dict):
        return None
    fetched_at = entry.get("fetched_at_epoch")
    if not isinstance(fetched_at, (int, float)):
        return None
    if time.time() - float(fetched_at) > ttl:
        return None
    board_ids = entry.get("board_ids") or []
    if not isinstance(board_ids, list):
        return None
    return [str(b).strip() for b in board_ids if str(b).strip()]


def _cache_set_notice_board_ids(course_ids: list[str], board_ids: list[str]) -> None:
    key = _course_ids_cache_key(course_ids)
    if not key:
        return

    def updater(cache: dict[str, Any]) -> dict[str, Any]:
        cache.setdefault("version", 1)
        cache.setdefault("course_info", {})
        discovery = cache.setdefault("notice_board_discovery", {})
        if not isinstance(discovery, dict):
            discovery = {}
            cache["notice_board_discovery"] = discovery
        discovery[key] = {
            "fetched_at_epoch": time.time(),
            "fetched_at_iso": _utc_now_iso(),
            "board_ids": [str(b).strip() for b in board_ids if str(b).strip()],
        }
        return cache

    update_json_file(
        CACHE_PATH,
        default=_cache_default(),
        updater=updater,
        chmod_mode=0o600,
    )


async def _gather_limited(items: list[Any], worker: Any, *, limit: int | None = None) -> list[Any]:
    if not items:
        return []
    sem = asyncio.Semaphore(limit or _concurrency_limit())

    async def run_one(item: Any) -> Any:
        async with sem:
            return await worker(item)

    return list(await asyncio.gather(*(run_one(item) for item in items)))


@asynccontextmanager
async def klms_runtime(*, headless: bool = True, accept_downloads: bool = True) -> AsyncIterator[dict[str, Any]]:
    """
    Keep one authenticated Playwright context alive for an entire CLI command.
    """
    existing = _RUNTIME_CONTEXT.get()
    if existing is not None:
        yield {"auth_mode": _RUNTIME_AUTH_MODE.get()}
        return

    async with _authenticated_context(headless=headless, accept_downloads=accept_downloads) as (context, auth_mode):
        token_ctx = _RUNTIME_CONTEXT.set(context)
        token_mode = _RUNTIME_AUTH_MODE.set(auth_mode)
        try:
            yield {"auth_mode": auth_mode}
        finally:
            _RUNTIME_CONTEXT.reset(token_ctx)
            _RUNTIME_AUTH_MODE.reset(token_mode)


class KlmsAuthError(RuntimeError):
    pass


@dataclass(frozen=True)
class KlmsConfig:
    base_url: str
    dashboard_path: str
    course_ids: tuple[str, ...]
    notice_board_ids: tuple[str, ...]
    exclude_course_title_patterns: tuple[str, ...]


def _load_config() -> KlmsConfig:
    _ensure_private_dirs()

    if not CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"KLMS config not found at {CONFIG_PATH}. "
            "Run `kaist klms config set --base-url ...` or create the file."
        )

    import tomllib

    data = tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    base_url = str(data.get("base_url", "")).strip()
    if not base_url:
        raise ValueError(f"Missing required config key base_url in {CONFIG_PATH}")

    base_url = base_url.rstrip("/")

    dashboard_path = str(data.get("dashboard_path", "/my/")).strip() or "/my/"
    if not dashboard_path.startswith("/"):
        dashboard_path = "/" + dashboard_path

    course_ids_raw = data.get("course_ids", []) or []
    if not isinstance(course_ids_raw, list):
        raise ValueError("course_ids must be a list (e.g. [177688])")
    course_ids = tuple(str(x).strip() for x in course_ids_raw if str(x).strip())

    notice_board_ids_raw = data.get("notice_board_ids", []) or []
    if not isinstance(notice_board_ids_raw, list):
        raise ValueError("notice_board_ids must be a list (e.g. [1174096])")
    notice_board_ids = tuple(str(x).strip() for x in notice_board_ids_raw if str(x).strip())

    exclude_patterns_raw = data.get("exclude_course_title_patterns", []) or []
    if not isinstance(exclude_patterns_raw, list):
        raise ValueError("exclude_course_title_patterns must be a list of regex strings")
    exclude_course_title_patterns = tuple(
        str(x) for x in exclude_patterns_raw if isinstance(x, (str, int, float)) and str(x).strip()
    )

    return KlmsConfig(
        base_url=base_url,
        dashboard_path=dashboard_path,
        course_ids=course_ids,
        notice_board_ids=notice_board_ids,
        exclude_course_title_patterns=exclude_course_title_patterns,
    )


def _has_profile_session() -> bool:
    if not PROFILE_DIR.exists() or not PROFILE_DIR.is_dir():
        return False
    try:
        return any(PROFILE_DIR.iterdir())
    except OSError:
        return False


def _has_storage_state_session() -> bool:
    return STORAGE_STATE_PATH.exists()


def _active_auth_mode() -> Literal["profile", "storage_state", "none"]:
    if _has_profile_session():
        return "profile"
    if _has_storage_state_session():
        return "storage_state"
    return "none"


def _require_auth_artifact() -> None:
    _ensure_private_dirs()
    if _active_auth_mode() == "none":
        raise FileNotFoundError(_klms_login_help())


def _klms_login_help() -> str:
    return (
        "KLMS login state not found or expired.\n"
        f"Expected one of:\n  - Playwright profile: {PROFILE_DIR}\n  - Storage state: {STORAGE_STATE_PATH}\n"
        "Re-authenticate to refresh cookies:\n"
        "  kaist klms auth login"
    )


def _epoch_to_iso_utc(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _storage_state_cookie_stats() -> dict[str, Any] | None:
    if not STORAGE_STATE_PATH.exists():
        return None
    try:
        raw = json.loads(STORAGE_STATE_PATH.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        return {"read_error": str(e)}

    cookies = raw.get("cookies") or []
    now_epoch = time.time()
    exp_epochs = [
        float(c.get("expires"))
        for c in cookies
        if isinstance(c, dict)
        and isinstance(c.get("expires"), (int, float))
        and float(c.get("expires")) > 0
    ]
    if not exp_epochs:
        return {
            "cookie_count": len(cookies),
            "expiring_cookie_count": 0,
            "next_expiry_iso": None,
            "next_expiry_in_hours": None,
            "latest_expiry_iso": None,
        }

    next_exp = min(exp_epochs)
    latest_exp = max(exp_epochs)
    return {
        "cookie_count": len(cookies),
        "expiring_cookie_count": len(exp_epochs),
        "next_expiry_iso": _epoch_to_iso_utc(next_exp),
        "next_expiry_in_hours": round((next_exp - now_epoch) / 3600, 2),
        "latest_expiry_iso": _epoch_to_iso_utc(latest_exp),
    }


def _looks_logged_out(html: str) -> bool:
    # KLMS is Moodle-based; logged-out pages commonly include `notloggedin` and/or `page-login-*`.
    if re.search(r'\bnotloggedin\b', html, re.IGNORECASE):
        return True
    if re.search(r'\bid=["\']page-login', html, re.IGNORECASE):
        return True
    # Some SSO flows land on pages that reference "ssologin".
    if re.search(r'\bssologin\b', html, re.IGNORECASE):
        return True
    return False


def _looks_login_url(url: str) -> bool:
    u = (url or "").lower()
    return any(
        needle in u
        for needle in (
            "/login/",
            "ssologin",
            "oidc",
            "sso",
        )
    )


def _raise_auth_error(*, final_url: str | None = None) -> None:
    msg = _klms_login_help()
    if final_url:
        msg += f"\nFinal URL: {final_url}"
    raise KlmsAuthError(msg)


def _is_video_filename(name: str) -> bool:
    return bool(re.search(r"\.(mp4|mkv|mov|avi|webm|m3u8|ts)$", name, re.IGNORECASE))


def _is_video_url(url: str) -> bool:
    return bool(re.search(r"(m3u8|dash|hls|stream|video)", url, re.IGNORECASE))


def _abs_url(base_url: str, maybe_relative: str) -> str:
    if maybe_relative.startswith("http://") or maybe_relative.startswith("https://"):
        return maybe_relative
    if not maybe_relative.startswith("/"):
        maybe_relative = "/" + maybe_relative
    return base_url.rstrip("/") + maybe_relative


def _sanitize_relpath(rel: str) -> Path:
    rel = (rel or "").strip().lstrip("/").replace("\\", "/")
    rel = re.sub(r"/+", "/", rel)
    parts = [p for p in rel.split("/") if p not in ("", ".", "..")]
    return Path(*parts)


def _same_origin(url_a: str, url_b: str) -> bool:
    try:
        a = urlparse(url_a)
        b = urlparse(url_b)
        return (a.scheme, a.netloc) == (b.scheme, b.netloc)
    except Exception:
        return False


def _detach_listener(emitter: Any, event_name: str, handler: Any) -> None:
    remove = getattr(emitter, "remove_listener", None) or getattr(emitter, "off", None)
    if callable(remove):
        try:
            remove(event_name, handler)
        except Exception:
            pass


def _endpoint_canonical_key(method: str, url: str) -> str:
    parsed = urlparse(url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    info = ",".join(sorted(query.get("info", [])))
    path = parsed.path or "/"
    if info:
        return f"{method.upper()} {path}?info={info}"
    return f"{method.upper()} {path}"


def _extract_methodname_from_post_data_preview(preview: str) -> str | None:
    text = (preview or "").strip()
    if not text:
        return None
    if not text.startswith("["):
        return None
    try:
        data = json.loads(text)
    except Exception:
        return None
    if isinstance(data, list) and data:
        first = data[0]
        if isinstance(first, dict):
            m = first.get("methodname")
            if isinstance(m, str) and m.strip():
                return m.strip()
    return None


def _summarize_json_shape(value: Any, *, depth: int = 0) -> Any:
    if depth >= 4:
        return {"type": type(value).__name__}
    if isinstance(value, dict):
        keys = list(value.keys())
        sample: dict[str, Any] = {}
        for k in keys[:5]:
            sample[str(k)] = _summarize_json_shape(value[k], depth=depth + 1)
        return {
            "type": "object",
            "key_count": len(keys),
            "keys": [str(k) for k in keys[:20]],
            "sample": sample,
        }
    if isinstance(value, list):
        return {
            "type": "array",
            "length": len(value),
            "item_shape": _summarize_json_shape(value[0], depth=depth + 1) if value else None,
        }
    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        return {"type": "boolean"}
    if isinstance(value, (int, float)):
        return {"type": "number"}
    if isinstance(value, str):
        return {"type": "string", "length": len(value)}
    return {"type": type(value).__name__}


def _classify_endpoint(endpoint: dict[str, Any]) -> dict[str, Any]:
    method = str(endpoint.get("method", "")).upper()
    url = str(endpoint.get("url", ""))
    parsed = urlparse(url)
    path = parsed.path or "/"
    query = parse_qs(parsed.query, keep_blank_values=True)
    info = ",".join(sorted(query.get("info", [])))
    methodname = _extract_methodname_from_post_data_preview(str(endpoint.get("post_data_preview") or ""))
    json_like = bool(endpoint.get("json_like"))
    content_types = [str(c).lower() for c in (endpoint.get("content_types") or [])]

    classification = {
        "canonical_key": _endpoint_canonical_key(method, url),
        "path": path,
        "info": info or None,
        "methodname": methodname,
        "category": "unknown",
        "confidence": 0.2,
        "recommended_for_cli": False,
        "reason": "No matching rule.",
    }

    if path == "/lib/ajax/service.php" and "core_course_get_recent_courses" in info:
        classification.update(
            {
                "category": "courses",
                "confidence": 0.95,
                "recommended_for_cli": True,
                "reason": "Core Moodle AJAX recent-courses endpoint.",
            }
        )
        return classification

    if methodname == "core_course_get_recent_courses":
        classification.update(
            {
                "category": "courses",
                "confidence": 0.95,
                "recommended_for_cli": True,
                "reason": "Detected by methodname in AJAX payload.",
            }
        )
        return classification

    if methodname and ("courseboard" in methodname or "notice" in methodname):
        classification.update(
            {
                "category": "notices",
                "confidence": 0.82,
                "recommended_for_cli": True,
                "reason": "Methodname indicates notice/courseboard data.",
            }
        )
        return classification

    if methodname and ("assign" in methodname or "calendar" in methodname):
        classification.update(
            {
                "category": "assignments",
                "confidence": 0.82,
                "recommended_for_cli": True,
                "reason": "Methodname indicates assignment/calendar event data.",
            }
        )
        return classification

    if path == "/lib/ajax/service.php" and "core_course_get_enrolled_courses_by_timeline_classification" in info:
        classification.update(
            {
                "category": "courses",
                "confidence": 0.9,
                "recommended_for_cli": True,
                "reason": "Core Moodle timeline/enrolled-courses endpoint.",
            }
        )
        return classification

    if path == "/lib/ajax/service.php" and "core_calendar_get_action_events_by_timesort" in info:
        classification.update(
            {
                "category": "calendar",
                "confidence": 0.85,
                "recommended_for_cli": True,
                "reason": "Core Moodle calendar events endpoint (potential assignment/deadline source).",
            }
        )
        return classification

    if path == "/lib/ajax/service.php" and "core_output_load_template_with_dependencies" in info:
        classification.update(
            {
                "category": "ui_template",
                "confidence": 0.7,
                "recommended_for_cli": False,
                "reason": "Template-rendering endpoint, likely presentation-focused.",
            }
        )
        return classification

    if path == "/lib/ajax/service-nologin.php":
        classification.update(
            {
                "category": "ui_template",
                "confidence": 0.6,
                "recommended_for_cli": False,
                "reason": "No-login AJAX template endpoint.",
            }
        )
        return classification

    if "/mod/assign/" in path:
        classification.update(
            {
                "category": "assignments",
                "confidence": 0.8 if json_like else 0.55,
                "recommended_for_cli": json_like,
                "reason": "Assignment module endpoint.",
            }
        )
        return classification

    if "/mod/courseboard/" in path:
        classification.update(
            {
                "category": "notices",
                "confidence": 0.8 if json_like else 0.6,
                "recommended_for_cli": json_like,
                "reason": "Courseboard/notice endpoint.",
            }
        )
        return classification

    if "/mod/resource/" in path or "pluginfile.php" in path:
        classification.update(
            {
                "category": "files",
                "confidence": 0.75 if json_like else 0.6,
                "recommended_for_cli": json_like,
                "reason": "Resource/file endpoint.",
            }
        )
        return classification

    if "/panopto/" in path or "video" in path:
        classification.update(
            {
                "category": "video",
                "confidence": 0.7,
                "recommended_for_cli": False,
                "reason": "Video integration endpoint (out of current non-video scope).",
            }
        )
        return classification

    if json_like or any("json" in ct for ct in content_types):
        classification.update(
            {
                "category": "json_unknown",
                "confidence": 0.4,
                "recommended_for_cli": False,
                "reason": "JSON-like endpoint; needs manual inspection.",
            }
        )
    return classification


@asynccontextmanager
async def _authenticated_context(
    *,
    headless: bool,
    accept_downloads: bool = False,
) -> AsyncIterator[tuple[Any, Literal["profile", "storage_state"]]]:
    _require_auth_artifact()
    mode = _active_auth_mode()
    config: KlmsConfig | None = None
    try:
        config = _load_config()
    except Exception:
        config = None

    _configure_playwright_env()
    from playwright.async_api import async_playwright  # type: ignore[import-untyped]

    async def launch_profile_context(playwright: Any) -> Any:
        try:
            return await playwright.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE_DIR),
                headless=headless,
                accept_downloads=accept_downloads,
            )
        except Exception as exc:
            if not _is_missing_browser_error(exc):
                raise
            await asyncio.to_thread(klms_install_browser, force=False)
            return await playwright.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE_DIR),
                headless=headless,
                accept_downloads=accept_downloads,
            )

    async def launch_browser(playwright: Any) -> Any:
        try:
            return await playwright.chromium.launch(headless=headless)
        except Exception as exc:
            if not _is_missing_browser_error(exc):
                raise
            await asyncio.to_thread(klms_install_browser, force=False)
            return await playwright.chromium.launch(headless=headless)

    async with async_playwright() as p:
        if mode == "profile":
            try:
                context = await launch_profile_context(p)
            except Exception:
                if not _has_storage_state_session():
                    raise
            else:
                use_profile = True
                # Some environments can open the profile but still get bounced to SSO.
                # If storage_state exists, probe profile quickly and fall back when needed.
                if config is not None and _has_storage_state_session():
                    use_profile = await _context_is_authenticated(context, config=config)
                if not use_profile:
                    await context.close()
                else:
                    try:
                        yield context, "profile"
                    finally:
                        await context.close()
                    return

        browser = await launch_browser(p)
        context = await browser.new_context(
            storage_state=str(STORAGE_STATE_PATH),
            accept_downloads=accept_downloads,
        )
        try:
            yield context, "storage_state"
        finally:
            await context.close()
            await browser.close()


@asynccontextmanager
async def _borrow_authenticated_context(
    *,
    headless: bool = True,
    accept_downloads: bool = False,
) -> AsyncIterator[tuple[Any, str]]:
    runtime_context = _RUNTIME_CONTEXT.get()
    runtime_mode = _RUNTIME_AUTH_MODE.get()
    if runtime_context is not None:
        yield runtime_context, str(runtime_mode or _active_auth_mode())
        return
    async with _authenticated_context(headless=headless, accept_downloads=accept_downloads) as (context, auth_mode):
        yield context, auth_mode


async def _fetch_html_and_url(
    path_or_url: str,
    *,
    timeout_ms: int = 20_000,
    allow_login_page: bool = False,
) -> tuple[str, str]:
    config = _load_config()
    _require_auth_artifact()

    url = _abs_url(config.base_url, path_or_url)
    async with _borrow_authenticated_context(headless=True) as (context, _auth_mode):
        page = await context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            html = await page.content()
            final_url = page.url
        finally:
            await page.close()
    if not allow_login_page and (_looks_logged_out(html) or _looks_login_url(final_url)):
        _raise_auth_error(final_url=final_url)
    return html, final_url


async def _fetch_html(path_or_url: str, *, timeout_ms: int = 20_000, allow_login_page: bool = False) -> str:
    html, _final_url = await _fetch_html_and_url(
        path_or_url,
        timeout_ms=timeout_ms,
        allow_login_page=allow_login_page,
    )
    return html


async def _context_is_authenticated(context: Any, *, config: KlmsConfig, timeout_ms: int = 10_000) -> bool:
    """
    Lightweight probe used to decide whether a candidate auth context is usable.
    """
    page = await context.new_page()
    try:
        await page.goto(_abs_url(config.base_url, config.dashboard_path), wait_until="domcontentloaded", timeout=timeout_ms)
        html = await page.content()
        final_url = page.url
    except Exception:
        return False
    finally:
        await page.close()
    return not (_looks_logged_out(html) or _looks_login_url(final_url))


async def _probe_auth(*, timeout_ms: int = 10_000) -> dict[str, Any]:
    """
    Best-effort online check for whether saved auth artifacts can reach the dashboard.
    Never raises; returns fields for `klms_status`.
    """
    out: dict[str, Any] = {
        "validated": False,
        "authenticated": None,
        "final_url": None,
        "error": None,
        "mode": _active_auth_mode(),
        "checked_at_iso": _utc_now_iso(),
    }

    if not CONFIG_PATH.exists() or out["mode"] == "none":
        return out

    try:
        config = _load_config()
        url = _abs_url(config.base_url, config.dashboard_path)
        async with _borrow_authenticated_context(headless=True) as (context, auth_mode):
            out["mode"] = auth_mode
            page = await context.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                html = await page.content()
                out["final_url"] = page.url
            finally:
                await page.close()

        out["validated"] = True
        out["authenticated"] = not (_looks_logged_out(html) or _looks_login_url(str(out["final_url"] or "")))
        return out
    except Exception as e:  # noqa: BLE001
        out["error"] = str(e)
        return out


def _extract_sesskey(html: str) -> str | None:
    patterns = [
        r'"sesskey"\s*:\s*"([^"]+)"',
        r"sesskey=([A-Za-z0-9]+)",
        r'name=["\']sesskey["\']\s+value=["\']([^"\']+)["\']',
    ]
    for pattern in patterns:
        m = re.search(pattern, html)
        if m:
            key = _norm_text(m.group(1))
            if key:
                return key
    return None


async def _moodle_ajax_call(context: Any, *, base_url: str, sesskey: str, methodname: str, args: dict[str, Any]) -> Any:
    url = _abs_url(base_url, f"/lib/ajax/service.php?sesskey={quote(sesskey)}&info={quote(methodname)}")
    payload = [{"index": 0, "methodname": methodname, "args": args}]
    resp = await context.request.post(
        url,
        data=json.dumps(payload),
        headers={"content-type": "application/json"},
        timeout=20_000,
    )
    text = await resp.text()
    if not (200 <= int(resp.status) < 300):
        raise ValueError(f"AJAX call failed ({resp.status}) for {methodname}")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"Failed parsing AJAX JSON for {methodname}: {e}") from e
    if not isinstance(parsed, list) or not parsed:
        raise ValueError(f"Unexpected AJAX response shape for {methodname}")
    first = parsed[0]
    if not isinstance(first, dict):
        raise ValueError(f"Unexpected AJAX item type for {methodname}")
    if bool(first.get("error")):
        raise ValueError(f"AJAX error payload for {methodname}: {first}")
    return first.get("data")


def _norm_text(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _parse_datetime_guess(raw: str) -> str | None:
    raw = _norm_text(raw)
    if not raw:
        return None

    # Moodle style: "Tuesday, 16 September 2025, 11:59 PM"
    # Normalize by stripping weekday and commas.
    moodle_raw = re.sub(r"^[A-Za-z]+,\s*", "", raw)
    moodle_raw = moodle_raw.replace(",", "")
    for fmt in [
        "%d %B %Y %I:%M %p",
        "%d %B %Y %H:%M",
    ]:
        try:
            dt = datetime.strptime(moodle_raw, fmt)
            return dt.isoformat(timespec="minutes")
        except ValueError:
            pass

    candidates = [
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%Y/%m/%d %H:%M",
        "%Y/%m/%d",
        "%Y.%m.%d %H:%M",
        "%Y.%m.%d",
    ]
    for fmt in candidates:
        try:
            dt = datetime.strptime(raw, fmt)
            # Treat as local time; encode without timezone to keep it portable.
            return dt.isoformat(timespec="minutes")
        except ValueError:
            continue
    return None


def _utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _iso_from_epoch(epoch: float | int | None) -> str | None:
    if not isinstance(epoch, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(float(epoch), tz=timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    except Exception:
        return None


def _tag_items(items: list[dict[str, Any]], *, source: str, confidence: float) -> list[dict[str, Any]]:
    tagged: list[dict[str, Any]] = []
    for item in items:
        row = dict(item)
        row["source"] = source
        row["confidence"] = float(confidence)
        tagged.append(row)
    return tagged


def _apply_limit(items: list[dict[str, Any]], limit: int | None) -> list[dict[str, Any]]:
    if limit is None:
        return items
    return items[: max(0, int(limit))]


def _apply_since_filter(items: list[dict[str, Any]], *, field: str, since_iso: str | None) -> list[dict[str, Any]]:
    if not since_iso:
        return items
    threshold = _norm_text(str(since_iso))
    if not threshold:
        return items
    filtered: list[dict[str, Any]] = []
    for item in items:
        value = _norm_text(str(item.get(field) or ""))
        if value and value >= threshold:
            filtered.append(item)
    return filtered


def _load_api_map() -> dict[str, Any] | None:
    if not API_MAP_PATH.exists():
        return None
    try:
        data = json.loads(API_MAP_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _recommended_methodnames_for_category(category: str) -> list[str]:
    api_map = _load_api_map()
    if not api_map:
        return []
    out: list[str] = []
    for endpoint in api_map.get("recommended_endpoints") or []:
        if not isinstance(endpoint, dict):
            continue
        if str(endpoint.get("category") or "") != category:
            continue
        methodname = endpoint.get("methodname")
        if isinstance(methodname, str) and methodname.strip():
            out.append(methodname.strip())
    return list(dict.fromkeys(out))


def _load_snapshot() -> dict[str, Any]:
    _ensure_private_dirs()
    return read_json_file(
        SNAPSHOT_PATH,
        default={"version": 1, "last_sync_iso": None, "courses": {}, "boards": {}},
    )


def _save_snapshot(snapshot: dict[str, Any]) -> None:
    _ensure_private_dirs()
    write_json_file_atomic(SNAPSHOT_PATH, snapshot, chmod_mode=0o600)


def _find_table_by_headers(soup: BeautifulSoup, header_keywords: list[str]) -> tuple[list[str], Any] | None:
    for table in soup.find_all("table"):
        headers: list[str] = []
        thead = table.find("thead")
        if thead:
            headers = [_norm_text(th.get_text(" ", strip=True)) for th in thead.find_all(["th", "td"])]
        if not headers:
            first_row = table.find("tr")
            if first_row:
                headers = [_norm_text(cell.get_text(" ", strip=True)) for cell in first_row.find_all(["th", "td"])]

        if not headers:
            continue

        header_line = " | ".join(h.lower() for h in headers if h)
        if all(any(k.lower() in header_line for k in group.split("|")) for group in header_keywords):
            return headers, table
    return None


def _extract_title_from_course_page(html: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    # Moodle-ish: h1 inside page header.
    for selector in [
        ("div", {"class": re.compile(r"page-header-headings")}),
        ("h1", {}),
        ("title", {}),
    ]:
        el = soup.find(selector[0], selector[1])
        if el:
            text = _norm_text(el.get_text(" ", strip=True))
            if text:
                return re.sub(r"^\s*Course:\s*", "", text, flags=re.IGNORECASE).strip()
    return None


def _extract_course_code_from_resource_index(html: str) -> str | None:
    """
    Best-effort extraction of a course code/shortname from the Files/Resources index page.

    Example <title>: "CS371_2025_3: Files"
    """
    soup = BeautifulSoup(html, "html.parser")
    title = None
    if soup.title:
        title = _norm_text(soup.title.get_text(" ", strip=True))
    if not title:
        return None

    # Pattern: "<CODE>: Files"
    m = re.match(r"^([^:]+)\s*:\s*Files\s*$", title)
    if m:
        code = _norm_text(m.group(1))
        # Avoid returning generic titles.
        if code and code.lower() not in {"files", "dashboard"}:
            return code

    # Fallback: pick the first token that looks code-ish.
    m = re.search(r"\b[A-Z]{2,}\d{2,}[A-Z0-9_]*\b", title)
    return m.group(0) if m else None


def _split_person_names(raw: str) -> list[str]:
    text = _norm_text(raw)
    if not text:
        return []
    text = re.sub(r"^(professors?|instructors?|teachers?|담당교수|교수진)\s*[:：]?\s*", "", text, flags=re.IGNORECASE)
    chunks = [c.strip() for c in re.split(r"[,/;|·\n]+", text) if c.strip()]
    out: list[str] = []
    for chunk in chunks:
        normalized = _norm_text(chunk)
        if not normalized:
            continue
        if "@" in normalized:
            continue
        if re.fullmatch(r"[0-9\-\+\(\)\s]{6,}", normalized):
            continue
        if len(normalized) > 80:
            continue
        out.append(normalized)
    return list(dict.fromkeys(out))


def _extract_professors_from_course_page(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    names: list[str] = []

    role_hint = re.compile(r"^(professors?|instructors?|teachers?|담당교수|교수진|교강사)\s*$", re.IGNORECASE)
    role_in_key = re.compile(r"(professor|instructor|teacher|담당교수|교수진|교강사)", re.IGNORECASE)
    assistant_hint = re.compile(r"(assistant|ta|조교)", re.IGNORECASE)

    # KLMS commonly exposes a dedicated "Professors" label in the course header.
    role_labels = [
        node
        for node in soup.find_all(string=True)
        if role_hint.match(_norm_text(str(node)))
    ]
    for label in role_labels:
        container = label.parent
        if container is None:
            continue
        block = container.parent if getattr(container.parent, "name", None) else container
        if block is None:
            continue
        anchors = list(block.find_all("a"))
        if len(anchors) > 8:
            # Avoid broad navigation blocks that happen to contain role text.
            continue
        for anchor in anchors:
            value = _norm_text(anchor.get_text(" ", strip=True))
            if not value:
                continue
            if "@" in value or assistant_hint.search(value):
                continue
            names.extend(_split_person_names(value))
        if not anchors:
            names.extend(_split_person_names(block.get_text(" ", strip=True)))

    # Fallback for table-style metadata rows.
    if not names:
        for row in soup.find_all("tr"):
            th = row.find("th")
            td = row.find("td")
            if not th or not td:
                continue
            key = _norm_text(th.get_text(" ", strip=True))
            if not key or not role_in_key.search(key):
                continue
            names.extend(_split_person_names(td.get_text(" ", strip=True)))

    deduped = list(dict.fromkeys([n for n in names if n and not assistant_hint.search(n)]))
    return deduped[:8]


def _course_code_base(course_code: str | None) -> str | None:
    """
    Normalize a KLMS course code to a stable base identifier for labels.

    Examples:
      - CS371_2025_3 -> CS371
      - CS492(C)_2025_3 -> CS492(C)
      - AE495(AT)_2025_4 -> AE495(AT)
    """
    if not course_code:
        return None
    s = course_code.strip()
    # Strip trailing "_YYYY_N" suffix.
    s = re.sub(r"_20\d{2}_\d+\s*$", "", s)
    return s or None


async def _get_course_info(course_id: str, *, use_cache: bool = True) -> dict[str, Any]:
    """
    Return a best-effort course metadata bundle for labeling and file organization.
    """
    if use_cache:
        cached = _cache_get_course_info(course_id)
        if cached and isinstance(cached.get("professors"), list):
            return cached

    config = _load_config()
    title = None
    code = None
    professors: list[str] = []
    try:
        course_html = await _fetch_html(f"/course/view.php?id={course_id}&section=0")
        title = _extract_title_from_course_page(course_html)
        professors = _extract_professors_from_course_page(course_html)
    except KlmsAuthError:
        raise
    except Exception:
        pass
    try:
        files_html = await _fetch_html(f"/mod/resource/index.php?id={course_id}")
        code = _extract_course_code_from_resource_index(files_html)
    except KlmsAuthError:
        raise
    except Exception:
        pass
    result = {
        "course_id": str(course_id),
        "course_title": title or f"course-{course_id}",
        "course_code": code,  # may be None
        "course_code_base": _course_code_base(code),
        "course_url": _abs_url(config.base_url, f"/course/view.php?id={course_id}"),
        "professors": professors,
    }
    if use_cache:
        _cache_set_course_info(course_id, result)
    return result


def _discover_courses_from_dashboard(html: str, base_url: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    courses: dict[str, dict[str, Any]] = {}

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "course/view.php" not in href:
            continue
        m = re.search(r"[?&]id=(\d+)", href)
        if not m:
            continue
        course_id = m.group(1)
        title = _norm_text(a.get_text(" ", strip=True))
        if not title:
            title = f"course-{course_id}"
        courses[course_id] = {
            "id": course_id,
            "title": title,
            "url": _abs_url(base_url, href),
        }

    return list(courses.values())


def _is_noise_course(title: str, exclude_patterns: tuple[str, ...]) -> bool:
    t = (title or "").strip()
    if not t:
        return True
    # Built-in defaults (can be overridden/extended via config).
    default_patterns = (
        r"^Exam Bank$",
        r"^Micro Learning$",
        r"^Teaching Skills$",
        r"^Learning Skills$",
        r"^How to use Panopto$",
        r"Panopto",
        r"Guide to KLMS",
        r"How to use KLMS",
    )
    patterns = default_patterns + tuple(exclude_patterns)
    for pat in patterns:
        try:
            if re.search(pat, t, flags=re.IGNORECASE):
                return True
        except re.error:
            # Ignore invalid user-provided regex patterns.
            continue
    return False


def _extract_current_term_from_dashboard(html: str) -> dict[str, Any] | None:
    """
    Extract currently selected (year, semester) from KLMS dashboard.

    KLMS uses <select name="year"> and <select name="semester"> in the dashboard header.
    """
    soup = BeautifulSoup(html, "html.parser")
    year_select = soup.find("select", attrs={"name": "year"})
    sem_select = soup.find("select", attrs={"name": "semester"})
    if not year_select or not sem_select:
        return None

    def selected_option_text(sel) -> tuple[str | None, str | None]:
        opt = sel.find("option", selected=True)
        if not opt:
            # Sometimes selection is represented via JS; fall back to first option.
            opt = sel.find("option")
        if not opt:
            return None, None
        return _norm_text(opt.get_text(" ", strip=True)), (opt.get("value") or None)

    year_text, year_value = selected_option_text(year_select)
    sem_text, sem_value = selected_option_text(sem_select)
    if not year_text or not sem_text:
        return None

    term_label = f"{year_text} {sem_text}"
    return {
        "year": year_text,
        "semester": sem_text,
        "year_value": year_value,
        "semester_value": sem_value,
        "term_label": term_label,
    }


def _extract_pagination_pages(soup: BeautifulSoup) -> list[int]:
    pages: set[int] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        # Common pagination patterns: page=2, page=1
        m = re.search(r"[?&]page=(\d+)", href)
        if m:
            pages.add(int(m.group(1)))
            continue
        m = re.search(r"[?&]p=(\d+)", href)
        if m:
            pages.add(int(m.group(1)))
            continue
    return sorted(pages)


def _discover_notice_board_ids_from_course_page(html: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    found: dict[str, dict[str, str]] = {}

    def in_header_like_region(el) -> bool:
        cur = el
        for _ in range(12):
            if not cur or not getattr(cur, "attrs", None):
                break
            classes = " ".join(cur.attrs.get("class", [])).lower()
            if any(
                k in classes
                for k in [
                    "ks-header",
                    "all-menu",
                    "tooltip-layer",
                    "breadcrumb",
                    "navbar",
                    "footer",
                    "menu",
                ]
            ):
                return True
            cur = cur.parent
        return False

    def looks_like_global_board(label: str, board_id: str, a_el) -> bool:
        # KLMS global boards commonly appear in header menus on many pages.
        global_board_ids = {"32044", "32045", "32047", "531193"}
        if board_id in global_board_ids:
            return True
        if a_el and a_el.get("target") == "_blank":
            return True
        if in_header_like_region(a_el):
            return True
        l = (label or "").lower()
        global_labels = {"notice", "guide to klms", "q&a", "faq"}
        if l in global_labels and "course" not in l and "강의" not in l:
            return True
        return False

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "mod/courseboard/view.php" not in href:
            continue
        m = re.search(r"[?&]id=(\d+)", href)
        if not m:
            continue
        board_id = m.group(1)
        label = _norm_text(a.get_text(" ", strip=True)) or "courseboard"
        if looks_like_global_board(label, board_id, a):
            continue
        found[board_id] = {"board_id": board_id, "label": label}

    return list(found.values())


async def _discover_notice_board_ids_for_courses(course_ids: list[str], *, use_cache: bool = True) -> list[str]:
    normalized_ids = [str(cid).strip() for cid in course_ids if str(cid).strip()]
    if not normalized_ids:
        return []

    if use_cache:
        cached = _cache_get_notice_board_ids(normalized_ids)
        if cached is not None:
            return list(dict.fromkeys(cached))

    async def discover_for_course(cid: str) -> list[str]:
        course_html = await _fetch_html(f"/course/view.php?id={cid}&section=0")
        return [b["board_id"] for b in _discover_notice_board_ids_from_course_page(course_html)]

    discovered_lists = await _gather_limited(normalized_ids, discover_for_course)
    merged: list[str] = []
    for ids in discovered_lists:
        merged.extend(ids)
    board_ids = list(dict.fromkeys(merged))

    if use_cache:
        _cache_set_notice_board_ids(normalized_ids, board_ids)
    return board_ids


def _extract_notice_id_from_href(href: str) -> str | None:
    # courseboard articles look like: article.php?id=<board_id>&bwid=<post_id>
    m = re.search(r"[?&]bwid=(\d+)", href)
    if m:
        return m.group(1)
    m = re.search(r"[?&]id=(\d+)", href)
    return m.group(1) if m else None


def _clean_html_text(raw: str) -> str:
    decoded = _html.unescape(raw or "")
    decoded = re.sub(r"<[^>]+>", " ", decoded)
    return _norm_text(decoded)


def _looks_like_video_item(title: str, url: str) -> bool:
    t = (title or "").lower()
    u = (url or "").lower()
    keywords = [
        "video",
        "lecture video",
        "panopto",
        "동영상",
        "영상",
    ]
    return any(k in t for k in keywords) or any(k in u for k in ["panopto", "m3u8", "hls", "stream"])


def _material_kind_from_module(module: str | None) -> str:
    return {
        "resource": "file",
        "folder": "folder",
        "url": "link",
        "page": "page",
    }.get(module or "", "unknown")


def _toml_quote(s: str) -> str:
    escaped = s.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _toml_array(values: list[str]) -> str:
    return "[" + ", ".join(_toml_quote(v) for v in values) + "]"


def klms_configure(
    base_url: str | None = None,
    *,
    dashboard_path: str | None = None,
    course_ids: list[str] | None = None,
    notice_board_ids: list[str] | None = None,
    exclude_course_title_patterns: list[str] | None = None,
    merge_existing: bool = True,
) -> dict[str, Any]:
    """
    Write KLMS config used by this CLI.

    Args:
        base_url: KLMS root URL, e.g. "https://klms.kaist.ac.kr". Optional if merging into existing config.
        dashboard_path: Dashboard path (defaults to /my/). Set None to keep existing when merging.
        course_ids: Optional explicit course IDs.
        notice_board_ids: Optional explicit notice board IDs.
        exclude_course_title_patterns: Optional regex patterns for filtering out non-course tiles.
        merge_existing: Keep unspecified fields from existing config when possible.
    """
    _ensure_private_dirs()

    existing: dict[str, Any] = {}
    if merge_existing and CONFIG_PATH.exists():
        import tomllib

        existing = tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8"))

    normalized_base_url: str
    if base_url is None:
        normalized_base_url = str(existing.get("base_url", "")).strip().rstrip("/")
        if not normalized_base_url:
            raise ValueError(
                "base_url is required for initial setup.\n"
                "Run: kaist klms config set --base-url https://klms.kaist.ac.kr"
            )
    else:
        normalized_base_url = base_url.strip().rstrip("/")
    if not normalized_base_url.startswith("http://") and not normalized_base_url.startswith("https://"):
        raise ValueError("base_url must start with http:// or https://")

    resolved_dashboard = dashboard_path
    if resolved_dashboard is None:
        resolved_dashboard = str(existing.get("dashboard_path", "/my/")).strip() or "/my/"
    if not resolved_dashboard.startswith("/"):
        resolved_dashboard = "/" + resolved_dashboard

    resolved_course_ids = course_ids
    if resolved_course_ids is None:
        resolved_course_ids = [str(x).strip() for x in (existing.get("course_ids") or []) if str(x).strip()]

    resolved_notice_board_ids = notice_board_ids
    if resolved_notice_board_ids is None:
        resolved_notice_board_ids = [str(x).strip() for x in (existing.get("notice_board_ids") or []) if str(x).strip()]

    resolved_exclude_patterns = exclude_course_title_patterns
    if resolved_exclude_patterns is None:
        resolved_exclude_patterns = [
            str(x).strip()
            for x in (existing.get("exclude_course_title_patterns") or [])
            if str(x).strip()
        ]

    lines = [
        f"base_url = {_toml_quote(normalized_base_url)}",
        f"dashboard_path = {_toml_quote(resolved_dashboard)}",
        f"course_ids = {_toml_array(resolved_course_ids)}",
        f"notice_board_ids = {_toml_array(resolved_notice_board_ids)}",
        f"exclude_course_title_patterns = {_toml_array(resolved_exclude_patterns)}",
        "",
    ]
    CONFIG_PATH.write_text("\n".join(lines), encoding="utf-8")
    return {
        "ok": True,
        "config_path": str(CONFIG_PATH),
        "base_url": normalized_base_url,
        "dashboard_path": resolved_dashboard,
        "course_ids": resolved_course_ids,
        "notice_board_ids": resolved_notice_board_ids,
        "exclude_course_title_patterns": resolved_exclude_patterns,
    }


async def klms_status(validate: bool = True) -> dict[str, Any]:
    """
    Check whether KLMS is configured and has a saved login session.
    """
    _ensure_private_dirs()
    active_mode = _active_auth_mode()
    status: dict[str, Any] = {
        "config_path": str(CONFIG_PATH),
        "profile_path": str(PROFILE_DIR),
        "storage_state_path": str(STORAGE_STATE_PATH),
        "playwright_browsers_path": str(_configure_playwright_env()),
        "cache_path": str(CACHE_PATH),
        "download_root": str(DOWNLOAD_ROOT),
        "concurrency_limit": _concurrency_limit(),
        "configured": CONFIG_PATH.exists(),
        "auth_artifacts": {
            "profile": _has_profile_session(),
            "storage_state": _has_storage_state_session(),
            "active_mode": active_mode,
        },
        "has_session": active_mode != "none",
        "storage_state_cookie_stats": _storage_state_cookie_stats(),
    }
    if CONFIG_PATH.exists():
        try:
            status["config"] = {"base_url": _load_config().base_url}
        except Exception as e:  # noqa: BLE001
            status["config_error"] = str(e)
    if validate:
        status["auth"] = await _probe_auth()
    warnings: list[str] = []
    cookie_stats = status.get("storage_state_cookie_stats") or {}
    if isinstance(cookie_stats, dict):
        hours = cookie_stats.get("next_expiry_in_hours")
        if isinstance(hours, (int, float)):
            if float(hours) <= 2:
                warnings.append("Session cookies are near expiry (<2h). Re-run `kaist klms auth refresh` soon.")
            elif float(hours) <= 24:
                warnings.append("Session cookies may expire within 24h.")
    auth = status.get("auth") or {}
    if isinstance(auth, dict) and auth.get("validated") and not auth.get("authenticated"):
        warnings.append("Saved session exists but online validation failed. Run `kaist klms auth refresh`.")
    status["warnings"] = warnings
    return status


def klms_refresh_auth(base_url: str | None = None, *, validate: bool = True) -> dict[str, Any]:
    refreshed = klms_bootstrap_login(base_url)
    if not validate:
        return {"ok": True, "refreshed": refreshed, "validated": False}
    status = asyncio.run(klms_status(validate=True))
    auth = status.get("auth") or {}
    return {
        "ok": bool(isinstance(auth, dict) and auth.get("authenticated")),
        "refreshed": refreshed,
        "validated": True,
        "status": status,
    }


async def klms_auth_doctor(validate: bool = True) -> dict[str, Any]:
    _ensure_private_dirs()
    checks: list[dict[str, Any]] = []

    def record(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    record("private_root_exists", PRIVATE_ROOT.exists(), str(PRIVATE_ROOT))
    record("download_root_exists", DOWNLOAD_ROOT.exists(), str(DOWNLOAD_ROOT))
    record("config_exists", CONFIG_PATH.exists(), str(CONFIG_PATH))
    record("profile_exists", PROFILE_DIR.exists(), str(PROFILE_DIR))
    record("storage_state_exists", STORAGE_STATE_PATH.exists(), str(STORAGE_STATE_PATH))

    try:
        _load_config()
        record("config_parse", True, "config parsed successfully")
    except Exception as e:  # noqa: BLE001
        record("config_parse", False, str(e))

    status = await klms_status(validate=validate)
    auth = status.get("auth") or {}
    if validate and isinstance(auth, dict):
        record(
            "online_auth_validation",
            bool(auth.get("authenticated")),
            str(auth.get("final_url") or auth.get("error") or "no details"),
        )

    all_ok = all(bool(c.get("ok")) for c in checks)
    recommendations: list[str] = []
    if not status.get("has_session"):
        recommendations.append("Run `kaist klms auth login`.")
    if isinstance(auth, dict) and auth.get("validated") and not auth.get("authenticated"):
        recommendations.append("Run `kaist klms auth refresh`.")
    if status.get("warnings"):
        recommendations.extend([str(w) for w in status.get("warnings") or []])

    return {
        "ok": all_ok,
        "checks": checks,
        "status": status,
        "recommendations": recommendations,
    }


async def klms_fetch_html(path_or_url: str) -> dict[str, Any]:
    """
    Fetch raw HTML from KLMS using the saved session.

    This is a debugging tool to iterate on selectors when KLMS UI changes.
    """
    html = await _fetch_html(path_or_url, allow_login_page=True)
    return {"url": path_or_url, "html": html}


async def klms_extract_matches(path_or_url: str, pattern: str, max_matches: int = 20, context_chars: int = 120) -> dict[str, Any]:
    """
    Fetch HTML and return snippets around regex matches.

    Useful for debugging selector/URL patterns without pasting full HTML.
    """
    html = await _fetch_html(path_or_url)
    try:
        rx = re.compile(pattern)
    except re.error as e:  # noqa: BLE001
        raise ValueError(f"Invalid regex pattern: {e}") from e

    matches = []
    for m in rx.finditer(html):
        start = max(0, m.start() - context_chars)
        end = min(len(html), m.end() + context_chars)
        snippet = html[start:end]
        matches.append(
            {
                "match": m.group(0),
                "start": m.start(),
                "end": m.end(),
                "snippet": snippet,
            }
        )
        if len(matches) >= max_matches:
            break

    return {"url": path_or_url, "pattern": pattern, "count": len(matches), "matches": matches}


async def klms_list_courses(
    include_all: bool = False,
    *,
    enrich: bool = True,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """
    List courses.

    Uses config.course_ids if set, and fetches each course page title.
    """
    config = _load_config()
    # Prefer dashboard discovery (no hardcoding).
    try:
        dashboard_html = await _fetch_html(config.dashboard_path)
        term = _extract_current_term_from_dashboard(dashboard_html)
        discovered = _discover_courses_from_dashboard(dashboard_html, config.base_url)
        if discovered:
            if term:
                for c in discovered:
                    c["term_label"] = term["term_label"]
            if not include_all:
                discovered = [c for c in discovered if not _is_noise_course(str(c.get("title", "")), config.exclude_course_title_patterns)]
            if enrich:
                # Best-effort enrich with course_code for better labeling.
                async def enrich_course(course: dict[str, Any]) -> tuple[str | None, str | None, Exception | None]:
                    try:
                        info = await _get_course_info(str(course["id"]), use_cache=True)
                        return info.get("course_code"), info.get("course_code_base"), None
                    except KlmsAuthError as e:
                        return None, None, e
                    except Exception:
                        return None, None, None

                enrichment = await _gather_limited(discovered, enrich_course)
                for course, (course_code, course_code_base, err) in zip(discovered, enrichment):
                    if err:
                        raise err
                    course["course_code"] = course_code
                    course["course_code_base"] = course_code_base
            else:
                for course in discovered:
                    course["course_code"] = None
                    course["course_code_base"] = None
            return _apply_limit(_tag_items(discovered, source="html:dashboard", confidence=0.72), limit)
    except KlmsAuthError:
        raise
    except Exception:
        # Fall back to configured course_ids.
        pass

    if not config.course_ids:
        raise ValueError(
            "Could not discover courses from dashboard and no course_ids configured.\n"
            f"Add to {CONFIG_PATH}, e.g. course_ids = [177688], "
            "or set dashboard_path if your dashboard differs (default: /my/)."
        )

    if enrich:
        async def build_course(course_id: str) -> dict[str, Any]:
            info = await _get_course_info(course_id, use_cache=True)
            return {
                "id": course_id,
                "title": info["course_title"],
                "course_code": info.get("course_code"),
                "course_code_base": info.get("course_code_base"),
                "url": info["course_url"],
                "term_label": None,
            }

        courses = await _gather_limited(list(config.course_ids), build_course)
    else:
        courses = [
            {
                "id": course_id,
                "title": f"course-{course_id}",
                "course_code": None,
                "course_code_base": None,
                "url": _abs_url(config.base_url, f"/course/view.php?id={course_id}"),
                "term_label": None,
            }
            for course_id in config.course_ids
        ]
    if include_all:
        return _apply_limit(_tag_items(courses, source="config:course_ids", confidence=0.55), limit)
    filtered = [c for c in courses if not _is_noise_course(str(c.get("title", "")), config.exclude_course_title_patterns)]
    return _apply_limit(_tag_items(filtered, source="config:course_ids", confidence=0.55), limit)


async def klms_list_courses_api(include_all: bool = False, *, limit: int = 50) -> list[dict[str, Any]]:
    """
    Experimental: list courses via KLMS internal AJAX endpoint.
    """
    config = _load_config()
    _require_auth_artifact()
    limit = max(1, min(limit, 200))

    async def run_with_context(context: Any, auth_mode: str) -> list[dict[str, Any]]:
        loop = asyncio.get_running_loop()
        response_future: asyncio.Future[dict[str, Any]] = loop.create_future()
        response_tasks: list[asyncio.Task[Any]] = []
        result: dict[str, Any] | None = None
        timed_out = False

        page = await context.new_page()

        def on_response(resp: Any) -> None:
            req = resp.request
            if req.resource_type not in {"xhr", "fetch"}:
                return
            if "core_course_get_recent_courses" not in req.url:
                return
            if not _same_origin(req.url, config.base_url):
                return

            async def capture() -> None:
                try:
                    text = await resp.text()
                except Exception as e:  # noqa: BLE001
                    if not response_future.done():
                        response_future.set_exception(e)
                    return
                if not response_future.done():
                    response_future.set_result(
                        {
                            "ok": 200 <= int(resp.status) < 300,
                            "status": int(resp.status),
                            "url": req.url,
                            "method": req.method,
                            "post_data_preview": (req.post_data or "")[:400],
                            "text": text,
                        }
                    )

            response_tasks.append(asyncio.create_task(capture()))

        page.on("response", on_response)
        try:
            await page.goto(
                _abs_url(config.base_url, config.dashboard_path),
                wait_until="domcontentloaded",
                timeout=30_000,
            )
            if not response_future.done():
                await asyncio.wait_for(response_future, timeout=8.0)
            result = response_future.result()
        except asyncio.TimeoutError:
            # Cautious fallback if the AJAX request is not observed due to UI variant.
            timed_out = True
        finally:
            _detach_listener(page, "response", on_response)
            for task in response_tasks:
                if not task.done():
                    task.cancel()
            if response_tasks:
                await asyncio.gather(*response_tasks, return_exceptions=True)
            await page.close()

        if timed_out:
            return await klms_list_courses(include_all=include_all, enrich=False)
        if not isinstance(result, dict):
            raise ValueError("Unexpected browser result when calling core_course_get_recent_courses.")
        if not result.get("ok"):
            raise ValueError(f"AJAX call failed for core_course_get_recent_courses: {result}")

        text = str(result.get("text", ""))
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as e:
            raise ValueError(f"Failed parsing core_course_get_recent_courses response JSON: {e}") from e

        if not isinstance(payload, list) or not payload:
            raise ValueError("Unexpected response shape from core_course_get_recent_courses.")
        first = payload[0]
        if not isinstance(first, dict):
            raise ValueError("Unexpected response item type from core_course_get_recent_courses.")
        if bool(first.get("error")):
            raise ValueError(f"core_course_get_recent_courses returned error payload: {first}")

        data = first.get("data") or []
        if not isinstance(data, list):
            raise ValueError("Unexpected data payload from core_course_get_recent_courses.")
        if len(data) > limit:
            data = data[:limit]

        courses: list[dict[str, Any]] = []
        for row in data:
            if not isinstance(row, dict):
                continue
            cid = str(row.get("id") or "").strip()
            if not cid:
                continue
            title = _norm_text(
                str(row.get("fullname") or row.get("fullnamedisplay") or f"course-{cid}")
            )
            course_code = str(row.get("shortname") or "").strip() or None
            view_url = row.get("viewurl")
            if isinstance(view_url, str) and view_url.strip():
                url = view_url.strip()
            else:
                url = _abs_url(config.base_url, f"/course/view.php?id={cid}")

            item = {
                "id": cid,
                "title": title,
                "course_code": course_code,
                "course_code_base": _course_code_base(course_code),
                "url": url,
                "term_label": None,
                "source": "ajax:core_course_get_recent_courses",
                "confidence": 0.9,
                "auth_mode": auth_mode,
            }
            courses.append(item)

        if include_all:
            return courses
        return [c for c in courses if not _is_noise_course(str(c.get("title", "")), config.exclude_course_title_patterns)]

    async with _borrow_authenticated_context(headless=True, accept_downloads=False) as (context, auth_mode):
        return await run_with_context(context, auth_mode)


async def klms_get_current_term() -> dict[str, Any]:
    """
    Return the currently selected term (year + semester) from the KLMS dashboard.
    """
    config = _load_config()
    dashboard_html = await _fetch_html(config.dashboard_path)
    term = _extract_current_term_from_dashboard(dashboard_html)
    if not term:
        raise ValueError("Could not extract current term from dashboard HTML.")
    return term


async def klms_get_course_info(course_id: str) -> dict[str, Any]:
    """
    Fetch and return course metadata, including best-effort course_code/shortname.
    """
    return await _get_course_info(course_id)


def _dedupe_strings(values: list[str]) -> list[str]:
    return list(dict.fromkeys([v for v in values if isinstance(v, str) and v.strip()]))


def _extract_assignment_rows_from_calendar_data(data: Any, *, base_url: str) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []

    def push_list(items: Any) -> None:
        if isinstance(items, list):
            for it in items:
                if isinstance(it, dict):
                    candidates.append(it)

    if isinstance(data, dict):
        push_list(data.get("events"))
        push_list(data.get("data"))
        push_list(data.get("items"))
    elif isinstance(data, list):
        push_list(data)

    out: list[dict[str, Any]] = []
    for row in candidates:
        module = str(row.get("modulename") or row.get("modname") or "").lower()
        eventtype = str(row.get("eventtype") or row.get("name") or "").lower()
        if module and module != "assign":
            continue
        if "assign" not in module and "assignment" not in eventtype and "assign" not in eventtype:
            continue

        cid = str(row.get("courseid") or row.get("course_id") or "").strip() or None
        title = _norm_text(str(row.get("name") or row.get("title") or "assignment"))
        url = row.get("url") or row.get("viewurl") or row.get("view_url")
        due_epoch = row.get("timesort") or row.get("timestart") or row.get("timedue")
        due_iso = _iso_from_epoch(due_epoch)
        out.append(
            {
                "course_id": cid,
                "id": str(row.get("instance") or row.get("id") or "").strip() or None,
                "title": title,
                "url": _abs_url(base_url, str(url)) if isinstance(url, str) and url.strip() else None,
                "due_raw": str(row.get("formattedtime") or row.get("timestring") or "").strip() or None,
                "due_iso": due_iso,
            }
        )
    return out


async def _list_assignments_api(course_ids: list[str]) -> tuple[list[dict[str, Any]], str] | None:
    """
    Best-effort API path for assignments using Moodle calendar actions endpoint.
    Returns None when API path is unavailable so caller can use HTML fallback.
    """
    config = _load_config()
    methodnames = _dedupe_strings(
        [
            "core_calendar_get_action_events_by_timesort",
            *(_recommended_methodnames_for_category("calendar")),
            *(_recommended_methodnames_for_category("assignments")),
        ]
    )
    if not methodnames:
        return None

    async with _borrow_authenticated_context(headless=True, accept_downloads=False) as (context, _auth_mode):
        page = await context.new_page()
        try:
            await page.goto(
                _abs_url(config.base_url, config.dashboard_path),
                wait_until="domcontentloaded",
                timeout=20_000,
            )
            dashboard_html = await page.content()
        finally:
            await page.close()
        sesskey = _extract_sesskey(dashboard_html)
        if not sesskey:
            return None

        args_candidates = [
            {"limitnum": 200, "timesortfrom": 0},
            {"limitnum": 200, "timesortfrom": int(time.time()) - (180 * 24 * 3600)},
            {},
        ]
        for methodname in methodnames:
            for args in args_candidates:
                try:
                    data = await _moodle_ajax_call(
                        context,
                        base_url=config.base_url,
                        sesskey=sesskey,
                        methodname=methodname,
                        args=args,
                    )
                except Exception:
                    continue
                items = _extract_assignment_rows_from_calendar_data(data, base_url=config.base_url)
                if course_ids:
                    allowed = set(str(cid) for cid in course_ids)
                    items = [it for it in items if str(it.get("course_id") or "") in allowed]
                if items:
                    return _tag_items(items, source=f"api:{methodname}", confidence=0.82), methodname
    return None


def _extract_notice_rows_from_api_data(data: Any, *, base_url: str, board_id: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if isinstance(data, list):
        rows = [r for r in data if isinstance(r, dict)]
    elif isinstance(data, dict):
        for key in ("posts", "items", "data", "articles"):
            value = data.get(key)
            if isinstance(value, list):
                rows = [r for r in value if isinstance(r, dict)]
                break
    out: list[dict[str, Any]] = []
    for row in rows:
        title = _norm_text(str(row.get("title") or row.get("subject") or "notice"))
        url = row.get("url") or row.get("viewurl")
        post_id = str(row.get("id") or row.get("postid") or row.get("bwid") or "").strip() or None
        posted_iso = _iso_from_epoch(row.get("timecreated") or row.get("created"))
        out.append(
            {
                "board_id": board_id,
                "id": post_id,
                "title": title,
                "url": _abs_url(base_url, str(url)) if isinstance(url, str) and url.strip() else None,
                "posted_raw": None,
                "posted_iso": posted_iso,
            }
        )
    return out


async def _list_notices_api(board_ids: list[str], *, max_pages: int, stop_post_id: str | None) -> list[dict[str, Any]] | None:
    """
    Best-effort API path for notices based on discovered endpoint map.
    Returns None when no suitable API mapping exists.
    """
    config = _load_config()
    methodnames = _dedupe_strings(_recommended_methodnames_for_category("notices"))
    if not methodnames:
        return None

    async with _borrow_authenticated_context(headless=True, accept_downloads=False) as (context, _auth_mode):
        page = await context.new_page()
        try:
            await page.goto(
                _abs_url(config.base_url, config.dashboard_path),
                wait_until="domcontentloaded",
                timeout=20_000,
            )
            dashboard_html = await page.content()
        finally:
            await page.close()
        sesskey = _extract_sesskey(dashboard_html)
        if not sesskey:
            return None

        out: list[dict[str, Any]] = []
        for board_id in board_ids:
            for methodname in methodnames:
                args_candidates = [
                    {"id": int(board_id) if str(board_id).isdigit() else board_id, "page": 0, "perpage": max(1, max_pages) * 20},
                    {"boardid": int(board_id) if str(board_id).isdigit() else board_id},
                    {"id": board_id},
                    {},
                ]
                for args in args_candidates:
                    try:
                        data = await _moodle_ajax_call(
                            context,
                            base_url=config.base_url,
                            sesskey=sesskey,
                            methodname=methodname,
                            args=args,
                        )
                    except Exception:
                        continue
                    rows = _extract_notice_rows_from_api_data(data, base_url=config.base_url, board_id=str(board_id))
                    for row in rows:
                        out.append(row)
                        if stop_post_id and str(row.get("id") or "") == str(stop_post_id):
                            return _tag_items(out, source=f"api:{methodname}", confidence=0.75)
                    if rows:
                        break
        if out:
            return _tag_items(out, source="api:mapped_notices", confidence=0.75)
        return None


async def klms_list_assignments(
    course_id: str | None = None,
    *,
    limit: int | None = None,
    since_iso: str | None = None,
) -> list[dict[str, Any]]:
    """
    List assignments with due dates for a course (or all courses).

    For each assignment, returns:
    - id (if derivable), title, url
    - due_raw (string), due_iso (best-effort)
    - course_id
    """
    config = _load_config()
    if course_id:
        course_ids = [course_id]
    else:
        course_ids = [c["id"] for c in await klms_list_courses(enrich=False)]
    if not course_ids:
        raise ValueError(f"Pass course_id or configure course_ids in {CONFIG_PATH}")

    api_items = await _list_assignments_api(course_ids)
    if api_items is not None:
        tagged, _methodname = api_items
        return _apply_limit(_apply_since_filter(tagged, field="due_iso", since_iso=since_iso), limit)

    async def list_assignments_for_course(cid: str) -> list[dict[str, Any]]:
        url_path = f"/mod/assign/index.php?id={cid}"
        html = await _fetch_html(url_path)
        soup = BeautifulSoup(html, "html.parser")
        out: list[dict[str, Any]] = []

        # Find a table that includes "Due date" / "마감" and an assignment name/title column.
        found = _find_table_by_headers(
            soup,
            header_keywords=[
                "due|마감|기한|종료",
            ],
        )

        if not found:
            # Fallback: gather links that look like assignment views.
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if "mod/assign/view.php" in href:
                    title = _norm_text(a.get_text(" ", strip=True))
                    out.append(
                        {
                            "course_id": str(cid),
                            "title": title,
                            "url": _abs_url(config.base_url, href),
                            "id": None,
                            "due_raw": None,
                            "due_iso": None,
                        }
                    )
            return _tag_items(out, source="html:assign-index-fallback", confidence=0.62)

        headers, table = found
        headers_norm = [h.lower() for h in headers]

        def col_index(*needles: str) -> int | None:
            for n in needles:
                for i, h in enumerate(headers_norm):
                    if n in h:
                        return i
            return None

        name_i = col_index("assignment", "과제", "과제명", "name", "제목") or 0
        due_i = col_index("due", "마감", "기한", "종료")

        rows = table.find_all("tr")
        # If first row is header row (th), skip it.
        if rows and rows[0].find_all("th"):
            rows = rows[1:]

        for row in rows:
            cells = row.find_all(["td", "th"])
            if not cells or name_i >= len(cells):
                continue

            name_cell = cells[name_i]
            link = name_cell.find("a", href=True)
            title = _norm_text(name_cell.get_text(" ", strip=True))
            href = link["href"] if link else None

            due_raw = None
            if due_i is not None and due_i < len(cells):
                due_raw = _norm_text(cells[due_i].get_text(" ", strip=True)) or None

            assignment_id = None
            if href:
                m = re.search(r"[?&]id=(\d+)", href)
                if m:
                    assignment_id = m.group(1)

            out.append(
                {
                    "course_id": str(cid),
                    "id": assignment_id,
                    "title": title,
                    "url": _abs_url(config.base_url, href) if href else None,
                    "due_raw": due_raw,
                    "due_iso": _parse_datetime_guess(due_raw) if due_raw else None,
                }
            )
        return _tag_items(out, source="html:assign-index", confidence=0.68)

    per_course = await _gather_limited(course_ids, list_assignments_for_course)
    all_items: list[dict[str, Any]] = []
    for items in per_course:
        all_items.extend(items)
    filtered = _apply_since_filter(all_items, field="due_iso", since_iso=since_iso)
    return _apply_limit(filtered, limit)


async def _resolve_notice_board_ids(explicit_board_id: str | None, config: KlmsConfig) -> list[str]:
    if explicit_board_id:
        return [str(explicit_board_id)]
    board_ids = list(config.notice_board_ids)
    if board_ids:
        return board_ids
    courses = await klms_list_courses(enrich=False)
    return await _discover_notice_board_ids_for_courses([str(c["id"]) for c in courses], use_cache=True)


def _plan_notice_page_sequence(first_soup: BeautifulSoup, max_pages: int) -> tuple[int, list[int]]:
    pages = [p for p in _extract_pagination_pages(first_soup) if p >= 0]
    if pages:
        first_page_index = 0 if 0 in pages else min(pages)
        sequence = [first_page_index] + [p for p in pages if p != first_page_index]
    else:
        first_page_index = 0
        sequence = [0, 1]
    return first_page_index, sequence[: max(0, max_pages)]


def _parse_notice_items_from_soup(
    soup: BeautifulSoup,
    *,
    board_id: str,
    base_url: str,
    fallback_url_path: str,
) -> list[dict[str, Any]]:
    table_info = _find_table_by_headers(
        soup,
        header_keywords=[
            "title|제목|subject",
        ],
    )
    if table_info:
        headers, table = table_info
        headers_norm = [h.lower() for h in headers]

        def col_index(*needles: str) -> int | None:
            for needle in needles:
                for i, header in enumerate(headers_norm):
                    if needle in header:
                        return i
            return None

        title_i = col_index("title", "제목", "subject") or 0
        date_i = col_index("date", "작성", "등록", "posted", "일자")
        rows = table.find_all("tr")
        if rows and rows[0].find_all("th"):
            rows = rows[1:]

        out: list[dict[str, Any]] = []
        for row in rows:
            cells = row.find_all(["td", "th"])
            if not cells or title_i >= len(cells):
                continue
            title_cell = cells[title_i]
            link = title_cell.find("a", href=True)
            title = _norm_text(title_cell.get_text(" ", strip=True))
            href = link["href"] if link else None
            posted_raw = None
            if date_i is not None and date_i < len(cells):
                posted_raw = _norm_text(cells[date_i].get_text(" ", strip=True)) or None

            out.append(
                {
                    "board_id": str(board_id),
                    "id": _extract_notice_id_from_href(href) if href else None,
                    "title": title,
                    "url": _abs_url(base_url, href) if href else _abs_url(base_url, fallback_url_path),
                    "posted_raw": posted_raw,
                    "posted_iso": _parse_datetime_guess(posted_raw) if posted_raw else None,
                }
            )
        return out

    out: list[dict[str, Any]] = []
    for link in soup.find_all("a", href=True):
        href = link["href"]
        if "mod/courseboard" not in href:
            continue
        title = _norm_text(link.get_text(" ", strip=True))
        if not title:
            continue
        out.append(
            {
                "board_id": str(board_id),
                "id": _extract_notice_id_from_href(href),
                "title": title,
                "url": _abs_url(base_url, href),
                "posted_raw": None,
                "posted_iso": None,
            }
        )
    return out


def _extract_notice_ids_from_url(url: str | None) -> tuple[str | None, str | None]:
    if not url:
        return None, None
    try:
        parsed = urlparse(url)
    except Exception:
        return None, None
    query = parse_qs(parsed.query, keep_blank_values=True)
    board_id = (query.get("id") or [None])[0]
    notice_id = (query.get("bwid") or [None])[0]
    board_id_s = str(board_id).strip() if isinstance(board_id, str) else None
    notice_id_s = str(notice_id).strip() if isinstance(notice_id, str) else None
    return (board_id_s or None), (notice_id_s or None)


def _looks_like_attachment_url(url: str) -> bool:
    parsed = urlparse(url)
    path = (parsed.path or "").lower()
    query = (parsed.query or "").lower()
    if "pluginfile.php" in path:
        return True
    if "forcedownload=1" in query:
        return True
    if "/mod/resource/" in path:
        return True
    return bool(
        re.search(
            r"\.(pdf|zip|7z|tar|gz|hwp|hwpx|doc|docx|ppt|pptx|xls|xlsx|txt|csv|py|ipynb)$",
            path,
            flags=re.IGNORECASE,
        )
    )


def _attachment_filename_from_url(url: str) -> str | None:
    try:
        path_name = Path(unquote(urlparse(url).path or "")).name
    except Exception:
        return None
    return path_name or None


def _extract_notice_title_from_soup(soup: BeautifulSoup) -> str | None:
    selectors = [
        "h1",
        "h2",
        "#page-header h1",
        ".subject",
        ".board-title",
        ".article-title",
        ".post-title",
    ]
    for selector in selectors:
        for node in soup.select(selector):
            title = _norm_text(node.get_text(" ", strip=True))
            if title:
                return title
    og_title = soup.select_one('meta[property="og:title"]')
    if og_title and isinstance(og_title.get("content"), str):
        title = _norm_text(str(og_title.get("content") or ""))
        if title:
            return title
    if soup.title:
        title = _norm_text(soup.title.get_text(" ", strip=True))
        if title:
            # Common Moodle title form: "<notice title> : <course>".
            if ":" in title:
                left = _norm_text(title.split(":", 1)[0])
                if left:
                    return left
            return title
    return None


def _extract_notice_meta_from_soup(soup: BeautifulSoup) -> tuple[str | None, str | None]:
    author: str | None = None
    posted_raw: str | None = None

    for row in soup.select("table tr"):
        th = row.find("th")
        td = row.find("td")
        if not th or not td:
            continue
        key = _norm_text(th.get_text(" ", strip=True)).lower()
        value = _norm_text(td.get_text(" ", strip=True))
        if not value:
            continue
        if not author and any(k in key for k in ("작성자", "author", "writer", "등록자")):
            author = value
        if not posted_raw and any(k in key for k in ("작성일", "등록일", "date", "posted", "시간")):
            posted_raw = value

    if not author or not posted_raw:
        for node in soup.select(".author, .writer, .user, .posted, .date, .time, .regdate, .info, .post-info"):
            text = _norm_text(node.get_text(" ", strip=True))
            if not text:
                continue
            if not author:
                m_author = re.search(r"(?:작성자|author|writer)\s*[:：]\s*(.+)$", text, flags=re.IGNORECASE)
                if m_author:
                    author = _norm_text(m_author.group(1))
            if not posted_raw:
                m_posted = re.search(r"(?:작성일|등록일|date|posted)\s*[:：]\s*(.+)$", text, flags=re.IGNORECASE)
                if m_posted:
                    posted_raw = _norm_text(m_posted.group(1))
                elif re.search(r"(20\d{2}[-./]\d{1,2}[-./]\d{1,2})", text):
                    posted_raw = text

    return author, posted_raw


def _select_notice_body_node(soup: BeautifulSoup) -> Any:
    selectors = [
        ".board_view .content",
        ".board_view .view-content",
        ".board_view .article-content",
        ".article-content",
        ".post-content",
        ".entry-content",
        "[class*=board][class*=content]",
        "#region-main .content",
        "#region-main",
        "article",
        "main",
    ]
    best_node: Any = None
    best_score = -1
    for selector in selectors:
        for node in soup.select(selector):
            text = _norm_text(node.get_text(" ", strip=True))
            if len(text) < 40:
                continue
            score = len(text)
            if node.find("p"):
                score += 40
            if len(node.find_all("a", href=True)) > 30:
                score -= 300
            if score > best_score:
                best_score = score
                best_node = node
    if best_node is not None:
        return best_node
    if soup.body:
        return soup.body
    return soup


def _collect_notice_attachments(soup: BeautifulSoup, *, base_url: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for link in soup.find_all("a", href=True):
        href = str(link.get("href") or "").strip()
        if not href or href.startswith("#") or href.lower().startswith("javascript:"):
            continue
        url = _abs_url(base_url, href)
        classes = " ".join(str(c) for c in (link.get("class") or [])).lower()
        if not _looks_like_attachment_url(url) and not any(token in classes for token in ("attach", "download", "file")):
            continue
        if url in seen_urls:
            continue
        seen_urls.add(url)
        title = _norm_text(link.get_text(" ", strip=True))
        filename = _attachment_filename_from_url(url)
        if not title:
            title = filename or url
        out.append(
            {
                "title": title,
                "url": url,
                "filename": filename,
                "is_video": _is_video_filename(filename or "") or _is_video_url(url),
            }
        )
    return out


def _parse_notice_detail_from_html(
    html: str,
    *,
    base_url: str,
    url: str | None = None,
    fallback_board_id: str | None = None,
    fallback_notice_id: str | None = None,
    include_html: bool = False,
) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()

    board_from_url, notice_from_url = _extract_notice_ids_from_url(url)
    board_id = fallback_board_id or board_from_url
    notice_id = fallback_notice_id or notice_from_url

    if not board_id or not notice_id:
        for link in soup.find_all("a", href=True):
            href = str(link.get("href") or "")
            if "mod/courseboard/article.php" not in href:
                continue
            bid, nid = _extract_notice_ids_from_url(href)
            board_id = board_id or bid
            notice_id = notice_id or nid
            if board_id and notice_id:
                break

    title = _extract_notice_title_from_soup(soup) or (f"notice-{notice_id}" if notice_id else "notice")
    author, posted_raw = _extract_notice_meta_from_soup(soup)
    posted_iso = _parse_datetime_guess(posted_raw) if posted_raw else None
    body_node = _select_notice_body_node(soup)
    body_text = _norm_text(body_node.get_text("\n", strip=True))
    attachments = _collect_notice_attachments(soup, base_url=base_url)

    result: dict[str, Any] = {
        "board_id": board_id,
        "id": notice_id,
        "title": title,
        "url": url,
        "author": author,
        "posted_raw": posted_raw,
        "posted_iso": posted_iso,
        "body_text": body_text or None,
        "attachments": attachments,
        "detail_available": bool(body_text),
        "source": "html:courseboard-article",
        "confidence": 0.78 if body_text else 0.62,
    }
    if include_html:
        result["body_html"] = str(body_node)
    return result


async def klms_list_notices(
    notice_board_id: str | None = None,
    *,
    max_pages: int = 1,
    stop_post_id: str | None = None,
    limit: int | None = None,
    since_iso: str | None = None,
) -> list[dict[str, Any]]:
    """
    List notices from one board or all configured/discovered boards.
    """
    config = _load_config()
    board_ids = await _resolve_notice_board_ids(notice_board_id, config)
    if not board_ids:
        raise ValueError(
            f"No notice boards found. Configure notice_board_ids in {CONFIG_PATH} "
            "or pass --notice-board-id."
        )

    api_items = await _list_notices_api(board_ids, max_pages=max_pages, stop_post_id=stop_post_id)
    if api_items is not None:
        return _apply_limit(_apply_since_filter(api_items, field="posted_iso", since_iso=since_iso), limit)

    all_items: list[dict[str, Any]] = []
    for board_id in board_ids:
        first_url_path = f"/mod/courseboard/view.php?id={board_id}"
        first_html = await _fetch_html(first_url_path)
        first_soup = BeautifulSoup(first_html, "html.parser")
        first_page_index, page_sequence = _plan_notice_page_sequence(first_soup, max_pages=max_pages)

        async def fetch_page(page_index: int) -> tuple[str, str]:
            if page_index == first_page_index:
                return first_url_path, first_html
            page_path = f"/mod/courseboard/view.php?id={board_id}&page={page_index}"
            return page_path, await _fetch_html(page_path)

        seen_keys: set[tuple[str, str, str]] = set()
        for page_index in page_sequence:
            page_url_path, page_html = await fetch_page(page_index)
            page_soup = BeautifulSoup(page_html, "html.parser")
            parsed = _parse_notice_items_from_soup(
                page_soup,
                board_id=str(board_id),
                base_url=config.base_url,
                fallback_url_path=page_url_path,
            )
            for post in parsed:
                pid = str(post.get("id") or "")
                url = str(post.get("url") or "")
                key = (str(board_id), pid, url)
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                tagged = dict(post)
                tagged["source"] = "html:courseboard"
                tagged["confidence"] = 0.66
                all_items.append(tagged)
                if stop_post_id and pid and pid == str(stop_post_id):
                    filtered = _apply_since_filter(all_items, field="posted_iso", since_iso=since_iso)
                    return _apply_limit(filtered, limit)

    filtered = _apply_since_filter(all_items, field="posted_iso", since_iso=since_iso)
    return _apply_limit(filtered, limit)


async def klms_get_notice(
    notice_id: str,
    *,
    notice_board_id: str | None = None,
    max_pages: int = 3,
    include_html: bool = False,
) -> dict[str, Any]:
    target_notice_id = str(notice_id).strip()
    if not target_notice_id:
        raise ValueError("notice_id is required")

    config = _load_config()
    board_ids = await _resolve_notice_board_ids(notice_board_id, config)
    if not board_ids:
        raise ValueError(
            f"No notice boards found. Configure notice_board_ids in {CONFIG_PATH} "
            "or pass --notice-board-id."
        )

    candidates: list[tuple[str, str, bool]] = []
    if notice_board_id:
        board_ids = [str(notice_board_id)]

    metadata_row: dict[str, Any] | None = None
    if not notice_board_id:
        try:
            rows = await klms_list_notices(max_pages=max_pages, limit=None)
            for row in rows:
                if str(row.get("id") or "") == target_notice_id:
                    metadata_row = dict(row)
                    break
        except Exception:
            metadata_row = None

    if metadata_row:
        board_from_row = str(metadata_row.get("board_id") or "").strip()
        url_from_row = str(metadata_row.get("url") or "").strip()
        if board_from_row and board_from_row in board_ids:
            board_ids = [board_from_row] + [bid for bid in board_ids if bid != board_from_row]
        if board_from_row and url_from_row:
            candidates.append((url_from_row, board_from_row, False))

    for board_id in board_ids:
        candidates.append(
            (
                _abs_url(config.base_url, f"/mod/courseboard/article.php?id={board_id}&bwid={target_notice_id}"),
                str(board_id),
                True,
            )
        )

    seen_urls: set[str] = set()
    for url, board_id, strict_article_url in candidates:
        if url in seen_urls:
            continue
        seen_urls.add(url)
        try:
            html, final_url = await _fetch_html_and_url(url)
        except KlmsAuthError:
            raise
        except Exception:
            continue

        if strict_article_url:
            final_url_l = final_url.lower()
            if "mod/courseboard/article.php" not in final_url_l:
                continue
            if f"bwid={target_notice_id}" not in final_url_l:
                continue

        detail = _parse_notice_detail_from_html(
            html,
            base_url=config.base_url,
            url=final_url,
            fallback_board_id=board_id,
            fallback_notice_id=target_notice_id,
            include_html=include_html,
        )
        parsed_notice_id = str(detail.get("id") or "").strip()
        if parsed_notice_id and parsed_notice_id != target_notice_id:
            continue

        detail["id"] = target_notice_id
        if metadata_row:
            merged = dict(metadata_row)
            merged.update(detail)
            return merged
        return detail

    if metadata_row:
        fallback = dict(metadata_row)
        fallback["detail_available"] = False
        fallback["body_text"] = None
        fallback["attachments"] = []
        if include_html:
            fallback["body_html"] = None
        return fallback
    raise FileNotFoundError(f"Notice not found: {notice_id}")


async def klms_sync_snapshot(
    *,
    update: bool = True,
    max_notice_pages: int = 3,
) -> dict[str, Any]:
    """
    Compute incremental changes since the last snapshot, optionally updating it.

    Returns:
      - assignments_new / assignments_updated
      - notices_new
      - materials_new
    """
    config = _load_config()
    snapshot = _load_snapshot()

    courses = await klms_list_courses(include_all=False, enrich=False)
    course_ids = [c["id"] for c in courses]

    # Assignments + materials per course.
    current_courses: dict[str, Any] = {}
    assignments_new: list[dict[str, Any]] = []
    assignments_updated: list[dict[str, Any]] = []
    materials_new: list[dict[str, Any]] = []

    async def collect_course_state(cid: str) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
        assignments, materials = await asyncio.gather(
            klms_list_assignments(cid),
            klms_list_files(cid),
        )
        return cid, assignments, materials

    course_states = await _gather_limited(course_ids, collect_course_state)
    for cid, assignments, materials in course_states:
        current_courses[cid] = {
            "assignments": {str(a.get("id")): a for a in assignments if a.get("id")},
            "materials": {str(m.get("url")): m for m in materials if m.get("url")},
        }

        prev_course = snapshot.get("courses", {}).get(cid, {})
        prev_assignments = prev_course.get("assignments", {}) or {}
        prev_materials = prev_course.get("materials", {}) or {}

        for aid, a in current_courses[cid]["assignments"].items():
            if aid not in prev_assignments:
                assignments_new.append(a)
            else:
                prev = prev_assignments[aid]
                if (
                    prev.get("due_iso") != a.get("due_iso")
                    or prev.get("title") != a.get("title")
                    or prev.get("url") != a.get("url")
                ):
                    assignments_updated.append({"before": prev, "after": a})

        for url, m in current_courses[cid]["materials"].items():
            if url not in prev_materials:
                materials_new.append(m)

    # Notices: prefer discovered boards (if any) otherwise config.
    board_ids = list(config.notice_board_ids)
    if not board_ids:
        board_ids = await _discover_notice_board_ids_for_courses(course_ids, use_cache=True)

    notices_new: list[dict[str, Any]] = []
    current_boards: dict[str, Any] = {}

    async def collect_board_state(bid: str) -> tuple[str, dict[str, Any], list[dict[str, Any]]]:
        prev_board = snapshot.get("boards", {}).get(bid, {})
        prev_posts = prev_board.get("posts", {}) or {}
        # Use any previously-seen post id as stop point to avoid paging forever.
        stop_post_id = None
        if prev_posts:
            # stop at the newest previously-seen post if we can infer it
            stop_post_id = next(iter(prev_posts.keys()))
        posts = await klms_list_notices(
            notice_board_id=bid,
            max_pages=max_notice_pages,
            stop_post_id=stop_post_id,
        )
        current_posts = {str(p.get("id")): p for p in posts if p.get("id")}
        new_posts: list[dict[str, Any]] = []
        for pid, p in current_posts.items():
            if pid not in prev_posts:
                new_posts.append(p)
        return bid, {"posts": current_posts}, new_posts

    board_states = await _gather_limited(board_ids, collect_board_state)
    for bid, board_data, new_posts in board_states:
        current_boards[bid] = board_data
        notices_new.extend(new_posts)

    result = {
        "snapshot_path": str(SNAPSHOT_PATH),
        "last_sync_before": snapshot.get("last_sync_iso"),
        "last_sync_after": _utc_now_iso(),
        "courses_considered": course_ids,
        "boards_considered": board_ids,
        "assignments_new": assignments_new,
        "assignments_updated": assignments_updated,
        "notices_new": notices_new,
        "materials_new": materials_new,
    }

    if update:
        snapshot["version"] = 1
        snapshot["last_sync_iso"] = result["last_sync_after"]
        snapshot["courses"] = {cid: current_courses[cid] for cid in current_courses}
        snapshot["boards"] = {bid: current_boards[bid] for bid in current_boards}
        _save_snapshot(snapshot)

    return result


async def klms_inbox(
    *,
    limit: int = 30,
    max_notice_pages: int = 1,
    since_iso: str | None = None,
) -> list[dict[str, Any]]:
    """
    Build a blended feed for daily checks (assignments + notices + files).
    """
    limit = max(1, min(int(limit), 500))

    assignments_task = klms_list_assignments(limit=max(limit * 2, 50), since_iso=since_iso)
    notices_task = klms_list_notices(max_pages=max_notice_pages, limit=max(limit * 2, 50), since_iso=since_iso)
    files_task = klms_list_files(limit=max(limit * 2, 50))
    assignments, notices, files = await asyncio.gather(assignments_task, notices_task, files_task)

    inbox: list[dict[str, Any]] = []
    for row in assignments:
        inbox.append(
            {
                "kind": "assignment",
                "id": row.get("id"),
                "title": row.get("title"),
                "url": row.get("url"),
                "course_id": row.get("course_id"),
                "time_iso": row.get("due_iso"),
                "due_iso": row.get("due_iso"),
                "source": row.get("source"),
                "confidence": row.get("confidence"),
            }
        )
    for row in notices:
        inbox.append(
            {
                "kind": "notice",
                "id": row.get("id"),
                "title": row.get("title"),
                "url": row.get("url"),
                "board_id": row.get("board_id"),
                "time_iso": row.get("posted_iso"),
                "posted_iso": row.get("posted_iso"),
                "source": row.get("source"),
                "confidence": row.get("confidence"),
            }
        )
    for row in files:
        inbox.append(
            {
                "kind": "file",
                "id": row.get("url"),
                "title": row.get("title"),
                "url": row.get("url"),
                "course_id": row.get("course_id"),
                "time_iso": None,
                "source": row.get("source"),
                "confidence": row.get("confidence"),
            }
        )

    # Sort by recency first, then keep stable kind priority (assignment -> notice -> file).
    inbox.sort(key=lambda item: (str(item.get("time_iso") or ""), str(item.get("title") or "")), reverse=True)
    inbox.sort(key=lambda item: {"assignment": 0, "notice": 1, "file": 2}.get(str(item.get("kind")), 9))
    return inbox[:limit]


async def klms_list_files(
    course_id: str | None = None,
    *,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """
    List downloadable files (excluding videos by default).

    Uses the Moodle resource index: /mod/resource/index.php?id=<course_id>.
    """
    config = _load_config()
    if course_id:
        course_ids = [course_id]
    else:
        course_ids = [c["id"] for c in await klms_list_courses(enrich=False)]
    if not course_ids:
        raise ValueError(f"Pass course_id or configure course_ids in {CONFIG_PATH}")

    async def list_files_for_course(cid: str) -> list[dict[str, Any]]:
        def extract_from_html(page_html: str) -> list[dict[str, Any]]:
            soup = BeautifulSoup(page_html, "html.parser")
            out: list[dict[str, Any]] = []

            for a in soup.find_all("a", href=True):
                href = a["href"]
                # Skip the resource index itself and obvious non-materials.
                if "/mod/resource/index.php" in href:
                    continue
                if "/mod/courseboard/" in href or "mod/courseboard/" in href:
                    continue
                if "/mod/assign/" in href or "mod/assign/" in href:
                    continue

                # Include modules likely to represent materials and direct files.
                module_match = re.search(r"/mod/([^/]+)/view\.php\?id=\d+", href)
                module = module_match.group(1) if module_match else None
                allowed_modules = {"resource", "folder", "url", "page"}
                is_module_view = bool(module and module in allowed_modules)
                is_direct_file = "pluginfile.php" in href
                if not (is_module_view or is_direct_file):
                    continue

                title = _norm_text(a.get_text(" ", strip=True))
                if not title:
                    title = _norm_text(a.get("title", "") or a.get("aria-label", "") or "")
                if not title:
                    title = re.sub(r"[?#].*$", "", href.rstrip("/").split("/")[-1])
                if not title:
                    continue

                url = _abs_url(config.base_url, href)
                if _is_video_url(url) or _is_video_filename(title) or _looks_like_video_item(title, url):
                    continue

                out.append(
                    {
                        "course_id": str(cid),
                        "title": title,
                        "url": url,
                        "kind": "file" if is_direct_file else _material_kind_from_module(module),
                        "is_video": False,
                    }
                )

            # KLMS course pages often embed downloadable materials via JS helper:
            # course.format.downloadFile('https://.../mod/resource/view.php?id=123', 'Title...<span ...')
            for m in re.finditer(
                r"downloadFile\(\s*['\"](https?://[^'\"]+?/mod/[^'\"]+?/view\.php\?id=\d+)['\"]\s*,\s*['\"](.+?)['\"]\s*\)",
                page_html,
                flags=re.DOTALL,
            ):
                url = m.group(1)
                title = _clean_html_text(m.group(2))
                if not title:
                    title = re.sub(r"[?#].*$", "", url.rstrip("/").split("/")[-1])
                module_match = re.search(r"/mod/([^/]+)/view\.php\?id=\d+", url)
                module = module_match.group(1) if module_match else None
                if module and module not in {"resource", "folder", "url", "page"}:
                    continue
                if _is_video_url(url) or _is_video_filename(title) or _looks_like_video_item(title, url):
                    continue
                out.append(
                    {
                        "course_id": str(cid),
                        "title": title,
                        "url": url,
                        "kind": _material_kind_from_module(module),
                        "is_video": False,
                    }
                )
            return out

        # Try resource index first (sometimes empty on KLMS).
        index_html = await _fetch_html(f"/mod/resource/index.php?id={cid}")
        per_course_items = extract_from_html(index_html)

        # Fallback: scan the course main page, which is where materials are often listed.
        # KLMS can hide older sections unless section=0 is used.
        if not per_course_items:
            course_html = await _fetch_html(f"/course/view.php?id={cid}&section=0")
            per_course_items = extract_from_html(course_html)

        return per_course_items

    per_course_items = await _gather_limited(course_ids, list_files_for_course)
    items: list[dict[str, Any]] = []
    for course_items in per_course_items:
        items.extend(course_items)

    # De-dupe by URL.
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for it in items:
        u = str(it.get("url") or "")
        if not u or u in seen:
            continue
        seen.add(u)
        deduped.append(it)
    tagged = _tag_items(deduped, source="html:resource-index", confidence=0.7)
    return _apply_limit(tagged, limit)


def _infer_course_id_for_download(resolved_url: str, subdir: str | None) -> str | None:
    if subdir:
        parts = list(_sanitize_relpath(subdir).parts)
        for part in reversed(parts):
            token = str(part).strip()
            if re.fullmatch(r"\d{4,}", token):
                return token

    try:
        parsed = urlparse(resolved_url)
        query = parse_qs(parsed.query, keep_blank_values=True)
    except Exception:
        return None

    for key in ("courseid", "course_id", "cid"):
        values = query.get(key) or []
        if values:
            candidate = str(values[0]).strip()
            if re.fullmatch(r"\d{4,}", candidate):
                return candidate

    path = (parsed.path or "").lower()
    if path.endswith("/course/view.php") or path.endswith("/mod/resource/index.php"):
        values = query.get("id") or []
        if values:
            candidate = str(values[0]).strip()
            if re.fullmatch(r"\d{4,}", candidate):
                return candidate
    return None


async def _term_label_for_course(course_id: str) -> str | None:
    try:
        courses = await klms_list_courses(include_all=True, enrich=False)
    except Exception:
        return None
    for row in courses:
        if str(row.get("id") or "") != str(course_id):
            continue
        term = row.get("term_label")
        if isinstance(term, str) and term.strip():
            return term.strip()
    return None


def _render_course_metadata_markdown(
    *,
    course_id: str,
    course_name: str,
    semester: str | None,
    course_code: str | None,
    course_code_base: str | None,
    professors: list[str],
    course_url: str | None,
) -> str:
    lines = [
        "# Course Metadata",
        "",
        f"- Course ID: `{course_id}`",
        f"- Course Name: {course_name}",
        f"- Semester: {semester or '(not detected)'}",
        f"- Course Code: {course_code or '(not detected)'}",
        f"- Course Code Base: {course_code_base or '(not detected)'}",
        f"- Professors: {', '.join(professors) if professors else '(not detected)'}",
        f"- KLMS URL: {course_url or '(not detected)'}",
        f"- Last Updated (UTC): {_utc_now_iso()}",
        "",
    ]
    return "\n".join(lines)


async def _write_course_metadata_markdown(course_id: str) -> Path:
    info = await _get_course_info(course_id, use_cache=False)
    term_label = await _term_label_for_course(course_id)
    professors = [str(p).strip() for p in (info.get("professors") or []) if str(p).strip()]
    markdown = _render_course_metadata_markdown(
        course_id=str(info.get("course_id") or course_id),
        course_name=str(info.get("course_title") or f"course-{course_id}"),
        semester=term_label,
        course_code=str(info.get("course_code")).strip() if info.get("course_code") else None,
        course_code_base=str(info.get("course_code_base")).strip() if info.get("course_code_base") else None,
        professors=professors,
        course_url=str(info.get("course_url")).strip() if info.get("course_url") else None,
    )
    course_dir = DOWNLOAD_ROOT / _sanitize_relpath(course_id)
    course_dir.mkdir(parents=True, exist_ok=True)
    doc_path = course_dir / "COURSE.md"
    doc_path.write_text(markdown, encoding="utf-8")
    return doc_path


async def klms_download_file(
    url: str,
    *,
    filename: str | None = None,
    subdir: str | None = None,
    if_exists: Literal["skip", "overwrite"] = "skip",
) -> dict[str, Any]:
    """
    Download a file under the configured download root.

    Args:
        url: Absolute or relative URL to the file
        filename: Optional filename override (otherwise derived from URL)
        subdir: Optional subdirectory under the download root (e.g. "2025 Winter/177688-Individual Study")
        if_exists: "skip" or "overwrite"
    """
    config = _load_config()
    _require_auth_artifact()
    _ensure_private_dirs()

    resolved_url = _abs_url(config.base_url, url)
    course_id_hint = _infer_course_id_for_download(resolved_url, subdir)
    if _is_video_url(resolved_url) or (filename and _is_video_filename(filename)):
        return {"skipped": True, "reason": "video", "url": resolved_url}

    async def run_with_context(context: Any, auth_mode: str) -> dict[str, Any]:
        # Preflight auth check so we fail fast (rather than timing out waiting for a download).
        auth_page = await context.new_page()
        try:
            await auth_page.goto(
                _abs_url(config.base_url, config.dashboard_path),
                wait_until="domcontentloaded",
                timeout=15_000,
            )
            auth_html = await auth_page.content()
            auth_final_url = auth_page.url
        finally:
            await auth_page.close()
        if _looks_logged_out(auth_html) or _looks_login_url(auth_final_url):
            _raise_auth_error(final_url=auth_final_url)

        page = await context.new_page()
        try:
            async with page.expect_download() as download_info:
                # Some KLMS resource URLs trigger downloads immediately, causing Playwright to raise:
                # "Page.goto: Download is starting". Treat that as success.
                try:
                    await page.goto(resolved_url, wait_until="commit", timeout=30_000)
                except Exception as e:  # noqa: BLE001
                    if "Download is starting" not in str(e):
                        raise
            download = await download_info.value
        finally:
            await page.close()

        suggested = download.suggested_filename
        final_name = filename or suggested or re.sub(r"[?#].*$", "", resolved_url.rstrip("/").split("/")[-1]) or f"download-{int(time.time())}"
        if _is_video_filename(final_name):
            return {
                "skipped": True,
                "reason": "video",
                "url": resolved_url,
                "filename": final_name,
                "auth_mode": auth_mode,
            }

        out_dir = DOWNLOAD_ROOT
        if subdir:
            out_dir = DOWNLOAD_ROOT / _sanitize_relpath(subdir)
            out_dir.mkdir(parents=True, exist_ok=True)

        out_path = out_dir / final_name

        course_metadata_path: str | None = None
        course_metadata_error: str | None = None

        async def ensure_course_metadata() -> None:
            nonlocal course_metadata_path, course_metadata_error
            if not course_id_hint:
                return
            try:
                doc_path = await _write_course_metadata_markdown(course_id_hint)
                course_metadata_path = str(doc_path)
            except Exception as exc:  # noqa: BLE001
                course_metadata_error = str(exc)

        if out_path.exists() and if_exists == "skip":
            await ensure_course_metadata()
            return {
                "skipped": True,
                "reason": "exists",
                "path": str(out_path),
                "url": resolved_url,
                "auth_mode": auth_mode,
                "course_id": course_id_hint,
                "course_metadata_path": course_metadata_path,
                "course_metadata_error": course_metadata_error,
            }

        await download.save_as(str(out_path))
        await ensure_course_metadata()
        return {
            "ok": True,
            "path": str(out_path),
            "url": resolved_url,
            "auth_mode": auth_mode,
            "course_id": course_id_hint,
            "course_metadata_path": course_metadata_path,
            "course_metadata_error": course_metadata_error,
        }

    async with _borrow_authenticated_context(headless=True, accept_downloads=True) as (context, auth_mode):
        return await run_with_context(context, auth_mode)


async def klms_discover_api(
    *,
    max_courses: int = 2,
    max_notice_boards: int = 2,
) -> dict[str, Any]:
    """
    Experimental: discover internal KLMS XHR/fetch endpoints from authenticated page loads.

    This does not mutate KLMS state. It only observes requests while visiting selected pages.
    """
    config = _load_config()
    _require_auth_artifact()
    _ensure_private_dirs()

    max_courses = max(0, min(max_courses, 10))
    max_notice_boards = max(0, min(max_notice_boards, 10))

    courses = await klms_list_courses(include_all=False, enrich=False)
    course_ids = [str(c["id"]) for c in courses][:max_courses]

    board_ids: list[str] = []
    if max_notice_boards > 0:
        board_ids = list(config.notice_board_ids)[:max_notice_boards]
        if not board_ids and course_ids:
            discovered = await _discover_notice_board_ids_for_courses(course_ids, use_cache=True)
            board_ids = discovered[:max_notice_boards]

    paths_to_visit: list[str] = [config.dashboard_path]
    for cid in course_ids:
        paths_to_visit.extend(
            [
                f"/course/view.php?id={cid}&section=0",
                f"/mod/assign/index.php?id={cid}",
                f"/mod/resource/index.php?id={cid}",
            ]
        )
    for bid in board_ids:
        paths_to_visit.append(f"/mod/courseboard/view.php?id={bid}")

    seen_paths: set[str] = set()
    deduped_paths: list[str] = []
    for path in paths_to_visit:
        if path in seen_paths:
            continue
        seen_paths.add(path)
        deduped_paths.append(path)

    captured: dict[str, dict[str, Any]] = {}
    base_url = config.base_url.rstrip("/")

    async def run_capture(context: Any, auth_mode: str) -> dict[str, Any]:
        response_tasks: list[asyncio.Task[Any]] = []

        def on_request(req: Any) -> None:
            if req.resource_type not in {"xhr", "fetch"}:
                return
            url = req.url
            if not _same_origin(url, base_url):
                return
            key = f"{req.method} {url}"
            item = captured.get(key)
            if item is None:
                item = {
                    "method": req.method,
                    "url": url,
                    "resource_type": req.resource_type,
                    "seen_count": 0,
                    "request_headers_subset": {
                        k: v
                        for k, v in (req.headers or {}).items()
                        if k.lower() in {"content-type", "accept", "x-requested-with", "referer"}
                    },
                    "has_post_data": bool(req.post_data),
                    "post_data_size": len(req.post_data or ""),
                    "post_data_preview": (req.post_data or "")[:400],
                    "status_codes": [],
                    "content_types": [],
                    "json_like": False,
                    "response_preview": "",
                    "response_json_shape": None,
                }
                captured[key] = item
            item["seen_count"] += 1

        async def capture_response_body(resp: Any, key: str, ctype: str) -> None:
            if "json" not in ctype.lower():
                return
            item = captured.get(key)
            if item is None:
                return
            if item.get("response_json_shape") is not None:
                return
            try:
                text = await resp.text()
            except Exception:
                return
            preview = text[:400]
            item["response_preview"] = preview
            try:
                parsed = json.loads(text)
                item["response_json_shape"] = _summarize_json_shape(parsed)
            except Exception:
                item["response_json_shape"] = {"type": "non-json-text", "length": len(text)}

        def on_response(resp: Any) -> None:
            req = resp.request
            if req.resource_type not in {"xhr", "fetch"}:
                return
            url = req.url
            if not _same_origin(url, base_url):
                return
            key = f"{req.method} {url}"
            item = captured.get(key)
            if item is None:
                return
            if resp.status not in item["status_codes"]:
                item["status_codes"].append(resp.status)
            ctype = (resp.headers or {}).get("content-type", "")
            if ctype and ctype not in item["content_types"]:
                item["content_types"].append(ctype)
            if "json" in ctype.lower():
                item["json_like"] = True
                response_tasks.append(asyncio.create_task(capture_response_body(resp, key, ctype)))

        context.on("request", on_request)
        context.on("response", on_response)

        visited: list[str] = []
        try:
            for path in deduped_paths:
                url = _abs_url(config.base_url, path)
                page = await context.new_page()
                try:
                    await page.goto(url, wait_until="networkidle", timeout=30_000)
                    visited.append(url)
                    # Trigger additional likely API calls by visiting a small set of page-local links.
                    link_candidates: list[str] = []
                    try:
                        anchors = await page.eval_on_selector_all(
                            "a[href]",
                            "els => els.map(el => el.href).filter(Boolean)",
                        )
                        if isinstance(anchors, list):
                            for href in anchors:
                                if not isinstance(href, str):
                                    continue
                                if not _same_origin(href, base_url):
                                    continue
                                if (
                                    "mod/assign/view.php" in href
                                    or "mod/courseboard/article.php" in href
                                    or "mod/resource/view.php" in href
                                ):
                                    link_candidates.append(href)
                        link_candidates = _dedupe_strings(link_candidates)[:3]
                    except Exception:
                        link_candidates = []

                    for href in link_candidates:
                        sub = await context.new_page()
                        try:
                            await sub.goto(href, wait_until="networkidle", timeout=20_000)
                            visited.append(href)
                        finally:
                            await sub.close()
                finally:
                    await page.close()
        finally:
            _detach_listener(context, "request", on_request)
            _detach_listener(context, "response", on_response)
            for task in response_tasks:
                if not task.done():
                    task.cancel()
            if response_tasks:
                await asyncio.gather(*response_tasks, return_exceptions=True)

        endpoints = list(captured.values())
        endpoints.sort(
            key=lambda e: (
                0 if e.get("json_like") else 1,
                -int(e.get("seen_count", 0)),
                e.get("url", ""),
            )
        )
        report = {
            "ok": True,
            "generated_at_iso": _utc_now_iso(),
            "base_url": config.base_url,
            "auth_mode": auth_mode,
            "visited_urls": visited,
            "course_ids_used": course_ids,
            "board_ids_used": board_ids,
            "endpoint_count": len(endpoints),
            "endpoints": endpoints,
        }
        write_json_file_atomic(ENDPOINT_DISCOVERY_PATH, report, chmod_mode=0o600)
        return {"ok": True, "report_path": str(ENDPOINT_DISCOVERY_PATH), **report}

    async with _borrow_authenticated_context(headless=True, accept_downloads=False) as (context, auth_mode):
        return await run_capture(context, auth_mode)


def klms_map_api(
    *,
    report_path: str | None = None,
) -> dict[str, Any]:
    """
    Build a categorized endpoint map from a discovery report.
    """
    _ensure_private_dirs()
    source = Path(report_path).expanduser() if report_path else ENDPOINT_DISCOVERY_PATH
    if not source.exists():
        raise FileNotFoundError(
            f"Discovery report not found at {source}. Run `kaist klms dev discover-api` first."
        )

    try:
        report = json.loads(source.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"Could not parse discovery report {source}: {e}") from e

    endpoints = report.get("endpoints") or []
    if not isinstance(endpoints, list):
        raise ValueError(f"Invalid discovery report format: endpoints must be a list ({source})")

    mapped: list[dict[str, Any]] = []
    by_canonical: dict[str, dict[str, Any]] = {}
    for endpoint in endpoints:
        if not isinstance(endpoint, dict):
            continue
        classification = _classify_endpoint(endpoint)
        canonical_key = classification["canonical_key"]

        existing = by_canonical.get(canonical_key)
        if existing is None:
            merged = {
                "method": endpoint.get("method"),
                "url": endpoint.get("url"),
                "path": classification["path"],
                "info": classification["info"],
                "methodname": classification.get("methodname"),
                "category": classification["category"],
                "confidence": classification["confidence"],
                "recommended_for_cli": classification["recommended_for_cli"],
                "reason": classification["reason"],
                "seen_count": int(endpoint.get("seen_count", 0) or 0),
                "status_codes": list(endpoint.get("status_codes") or []),
                "content_types": list(endpoint.get("content_types") or []),
                "json_like": bool(endpoint.get("json_like")),
                "request_headers_subset": endpoint.get("request_headers_subset") or {},
                "has_post_data": bool(endpoint.get("has_post_data")),
                "post_data_size": int(endpoint.get("post_data_size", 0) or 0),
                "post_data_preview": endpoint.get("post_data_preview") or "",
                "response_json_shape": endpoint.get("response_json_shape"),
                "response_preview": endpoint.get("response_preview"),
                "canonical_key": canonical_key,
            }
            by_canonical[canonical_key] = merged
            mapped.append(merged)
            continue

        existing["seen_count"] += int(endpoint.get("seen_count", 0) or 0)
        for code in endpoint.get("status_codes") or []:
            if code not in existing["status_codes"]:
                existing["status_codes"].append(code)
        for ctype in endpoint.get("content_types") or []:
            if ctype not in existing["content_types"]:
                existing["content_types"].append(ctype)
        existing["json_like"] = bool(existing["json_like"] or endpoint.get("json_like"))
        # Keep the higher-confidence classification if duplicates disagree.
        if float(classification["confidence"]) > float(existing["confidence"]):
            existing["category"] = classification["category"]
            existing["confidence"] = classification["confidence"]
            existing["recommended_for_cli"] = classification["recommended_for_cli"]
            existing["reason"] = classification["reason"]
            existing["info"] = classification["info"]
            existing["methodname"] = classification.get("methodname")

    mapped.sort(key=lambda e: (0 if e["recommended_for_cli"] else 1, -float(e["confidence"]), -int(e["seen_count"])))

    category_counts: dict[str, int] = {}
    recommended: list[dict[str, Any]] = []
    for item in mapped:
        category = str(item.get("category") or "unknown")
        category_counts[category] = category_counts.get(category, 0) + 1
        if item.get("recommended_for_cli"):
            recommended.append(item)

    output = {
        "ok": True,
        "generated_at_iso": _utc_now_iso(),
        "source_report_path": str(source),
        "endpoint_count_raw": len(endpoints),
        "endpoint_count_unique": len(mapped),
        "category_counts": category_counts,
        "recommended_count": len(recommended),
        "recommended_endpoints": recommended,
        "mapped_endpoints": mapped,
    }
    write_json_file_atomic(API_MAP_PATH, output, chmod_mode=0o600)
    return {"ok": True, "map_path": str(API_MAP_PATH), **output}


def klms_bootstrap_login(base_url: str | None = None) -> dict[str, Any]:
    """
    Open an interactive browser, let the user log in, then persist auth artifacts.
    """
    _configure_playwright_env()
    login_base_url = base_url.strip().rstrip("/") if base_url else _load_config().base_url
    from playwright.sync_api import sync_playwright  # type: ignore[import-untyped]

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(PROFILE_DIR, 0o700)
    except PermissionError:
        pass

    print(f"Opening browser to: {login_base_url}", file=sys.stderr)
    print("Log in fully, navigate to a course page, then return here and press Enter.", file=sys.stderr)
    with sync_playwright() as p:
        try:
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE_DIR),
                headless=False,
            )
        except Exception as exc:
            if not _is_missing_browser_error(exc):
                raise
            print("Playwright Chromium runtime not found; installing now...", file=sys.stderr)
            klms_install_browser(force=False)
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE_DIR),
                headless=False,
            )
        page = context.new_page()
        page.goto(login_base_url, wait_until="domcontentloaded", timeout=30_000)
        print("Press Enter to save session and exit... ", end="", file=sys.stderr, flush=True)
        input()
        # Keep storage_state as a fallback/debug artifact even when using profile mode.
        context.storage_state(path=str(STORAGE_STATE_PATH))
        context.close()

    try:
        os.chmod(STORAGE_STATE_PATH, 0o600)
    except PermissionError:
        pass

    return {
        "ok": True,
        "base_url": login_base_url,
        "profile_path": str(PROFILE_DIR),
        "storage_state_path": str(STORAGE_STATE_PATH),
        "preferred_mode": "profile",
    }


if __name__ == "__main__":
    raise SystemExit("This module is intended to be used via `kaist klms ...`.")
