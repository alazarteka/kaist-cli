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
import html as _html
import json
from contextlib import asynccontextmanager
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Literal
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup  # type: ignore[import-untyped]
from playwright.async_api import async_playwright  # type: ignore[import-untyped]

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
    if _has_storage_state_session():
        return "storage_state"
    if _has_profile_session():
        return "profile"
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
    return datetime.utcfromtimestamp(epoch).replace(microsecond=0).isoformat() + "Z"


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

    async with async_playwright() as p:
        if mode == "profile":
            try:
                context = await p.chromium.launch_persistent_context(
                    user_data_dir=str(PROFILE_DIR),
                    headless=headless,
                    accept_downloads=accept_downloads,
                )
            except Exception:
                if not _has_storage_state_session():
                    raise
            else:
                try:
                    yield context, "profile"
                finally:
                    await context.close()
                return

        browser = await p.chromium.launch(headless=headless)
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


async def _fetch_html(path_or_url: str, *, timeout_ms: int = 20_000, allow_login_page: bool = False) -> str:
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
    return html


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
    # Keep it timezone-agnostic; treat as informational.
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


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
        if cached:
            return cached

    config = _load_config()
    title = None
    code = None
    try:
        course_html = await _fetch_html(f"/course/view.php?id={course_id}&section=0")
        title = _extract_title_from_course_page(course_html)
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
    return status


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


async def klms_list_courses(include_all: bool = False, *, enrich: bool = True) -> list[dict[str, Any]]:
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
            return discovered
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
        return courses
    return [c for c in courses if not _is_noise_course(str(c.get("title", "")), config.exclude_course_title_patterns)]


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


async def klms_list_assignments(course_id: str | None = None) -> list[dict[str, Any]]:
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
            return out

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
        return out

    per_course = await _gather_limited(course_ids, list_assignments_for_course)
    all_items: list[dict[str, Any]] = []
    for items in per_course:
        all_items.extend(items)
    return all_items


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


async def klms_list_notices(
    notice_board_id: str | None = None,
    *,
    max_pages: int = 1,
    stop_post_id: str | None = None,
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
                all_items.append(post)
                if stop_post_id and pid and pid == str(stop_post_id):
                    return all_items

    return all_items


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


async def klms_list_files(course_id: str | None = None) -> list[dict[str, Any]]:
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
    return deduped


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
        if out_path.exists() and if_exists == "skip":
            return {
                "skipped": True,
                "reason": "exists",
                "path": str(out_path),
                "url": resolved_url,
                "auth_mode": auth_mode,
            }

        await download.save_as(str(out_path))
        return {"ok": True, "path": str(out_path), "url": resolved_url, "auth_mode": auth_mode}

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
    _ensure_private_dirs()
    login_base_url = base_url.strip().rstrip("/") if base_url else _load_config().base_url
    from playwright.sync_api import sync_playwright  # type: ignore[import-untyped]

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(PROFILE_DIR, 0o700)
    except PermissionError:
        pass

    print(f"Opening browser to: {login_base_url}")
    print("Log in fully, navigate to a course page, then return here and press Enter.")
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=False,
        )
        page = context.new_page()
        page.goto(login_base_url, wait_until="domcontentloaded", timeout=30_000)
        input("Press Enter to save session and exit... ")
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
        "preferred_mode": "storage_state",
    }


if __name__ == "__main__":
    raise SystemExit("This module is intended to be used via `kaist klms ...`.")
