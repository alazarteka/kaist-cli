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

from bs4 import BeautifulSoup  # type: ignore[import-untyped]
from playwright.async_api import async_playwright  # type: ignore[import-untyped]


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


def _ensure_private_dirs() -> None:
    PRIVATE_ROOT.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(PRIVATE_ROOT, 0o700)
    except PermissionError:
        pass
    DOWNLOAD_ROOT.mkdir(parents=True, exist_ok=True)


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
            "Run `kaist klms configure --base-url ...` or create the file."
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
        "  kaist klms login"
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
                try:
                    yield context, "profile"
                finally:
                    await context.close()
                return
            except Exception:
                if not _has_storage_state_session():
                    raise

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


async def _fetch_html(path_or_url: str, *, timeout_ms: int = 20_000, allow_login_page: bool = False) -> str:
    config = _load_config()
    _require_auth_artifact()

    url = _abs_url(config.base_url, path_or_url)

    async with _authenticated_context(headless=True) as (context, _auth_mode):
        page = await context.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        html = await page.content()
        final_url = page.url
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
        async with _authenticated_context(headless=True) as (context, auth_mode):
            out["mode"] = auth_mode
            page = await context.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            html = await page.content()
            out["final_url"] = page.url

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
    if not SNAPSHOT_PATH.exists():
        return {"version": 1, "last_sync_iso": None, "courses": {}, "boards": {}}
    try:
        return json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    except Exception:
        # Corrupt snapshot: start fresh but keep file for inspection.
        return {"version": 1, "last_sync_iso": None, "courses": {}, "boards": {}}


def _save_snapshot(snapshot: dict[str, Any]) -> None:
    _ensure_private_dirs()
    SNAPSHOT_PATH.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


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


async def _get_course_info(course_id: str) -> dict[str, Any]:
    """
    Return a best-effort course metadata bundle for labeling and file organization.
    """
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
    return {
        "course_id": str(course_id),
        "course_title": title or f"course-{course_id}",
        "course_code": code,  # may be None
        "course_code_base": _course_code_base(code),
        "course_url": _abs_url(config.base_url, f"/course/view.php?id={course_id}"),
    }


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
    base_url: str,
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
        base_url: KLMS root URL, e.g. "https://klms.kaist.ac.kr"
        dashboard_path: Dashboard path (defaults to /my/). Set None to keep existing when merging.
        course_ids: Optional explicit course IDs.
        notice_board_ids: Optional explicit notice board IDs.
        exclude_course_title_patterns: Optional regex patterns for filtering out non-course tiles.
        merge_existing: Keep unspecified fields from existing config when possible.
    """
    _ensure_private_dirs()
    normalized_base_url = base_url.strip().rstrip("/")
    if not normalized_base_url.startswith("http://") and not normalized_base_url.startswith("https://"):
        raise ValueError("base_url must start with http:// or https://")

    existing: dict[str, Any] = {}
    if merge_existing and CONFIG_PATH.exists():
        import tomllib

        existing = tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8"))

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
        "download_root": str(DOWNLOAD_ROOT),
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


async def klms_list_courses(include_all: bool = False) -> list[dict[str, Any]]:
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
            # Best-effort enrich with course_code for better labeling (ignore failures).
            for c in discovered:
                try:
                    info = await _get_course_info(str(c["id"]))
                    c["course_code"] = info.get("course_code")
                    c["course_code_base"] = info.get("course_code_base")
                except KlmsAuthError:
                    raise
                except Exception:
                    c["course_code"] = None
                    c["course_code_base"] = None
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

    courses: list[dict[str, Any]] = []
    for course_id in config.course_ids:
        info = await _get_course_info(course_id)
        courses.append(
            {
                "id": course_id,
                "title": info["course_title"],
                "course_code": info.get("course_code"),
                "course_code_base": info.get("course_code_base"),
                "url": info["course_url"],
                "term_label": None,
            }
        )
    if include_all:
        return courses
    return [c for c in courses if not _is_noise_course(str(c.get("title", "")), config.exclude_course_title_patterns)]


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
        course_ids = [c["id"] for c in await klms_list_courses()]
    if not course_ids:
        raise ValueError(f"Pass course_id or configure course_ids in {CONFIG_PATH}")

    all_items: list[dict[str, Any]] = []
    for cid in course_ids:
        url_path = f"/mod/assign/index.php?id={cid}"
        html = await _fetch_html(url_path)
        soup = BeautifulSoup(html, "html.parser")

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
                    all_items.append(
                        {
                            "course_id": str(cid),
                            "title": title,
                            "url": _abs_url(config.base_url, href),
                            "id": None,
                            "due_raw": None,
                            "due_iso": None,
                        }
                    )
            continue

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

            all_items.append(
                {
                    "course_id": str(cid),
                    "id": assignment_id,
                    "title": title,
                    "url": _abs_url(config.base_url, href) if href else None,
                    "due_raw": due_raw,
                    "due_iso": _parse_datetime_guess(due_raw) if due_raw else None,
                }
            )

    return all_items


async def klms_list_notices(
    course_id: str | None = None,
    *,
    max_pages: int = 1,
    stop_bwid: str | None = None,
) -> list[dict[str, Any]]:
    """
    List course notices/announcements (optionally filtered by course).

    KLMS notices are exposed via courseboard. For now, treat `course_id` as a
    courseboard id (the `id` value in mod/courseboard/view.php?id=...).
    If course_id is omitted, uses config.notice_board_ids.
    """
    config = _load_config()
    # If course_id is provided, treat it as a courseboard id for backwards-compat.
    if course_id:
        board_ids = [course_id]
    else:
        board_ids = list(config.notice_board_ids)
        if not board_ids:
            # Try to discover boards by scanning each course page.
            discovered: list[str] = []
            for c in await klms_list_courses():
                cid = c["id"]
                course_html = await _fetch_html(f"/course/view.php?id={cid}")
                for board in _discover_notice_board_ids_from_course_page(course_html):
                    discovered.append(board["board_id"])
            # De-dupe preserving order.
            seen: set[str] = set()
            board_ids = []
            for bid in discovered:
                if bid in seen:
                    continue
                seen.add(bid)
                board_ids.append(bid)
    if not board_ids:
        raise ValueError(
            f"No notice boards found. Configure notice_board_ids in {CONFIG_PATH} "
            "or pass course_id=<courseboard_id>."
        )

    items: list[dict[str, Any]] = []
    for board_id in board_ids:
        # Page numbers: KLMS uses pagination; we crawl from newest page forward until stop conditions.
        first_url_path = f"/mod/courseboard/view.php?id={board_id}"
        first_html = await _fetch_html(first_url_path)
        first_soup = BeautifulSoup(first_html, "html.parser")

        # Discover page indices from the first page.
        pages = _extract_pagination_pages(first_soup)
        # Include page 0/1 even if not linked.
        if pages:
            # Ensure we include the currently viewed page index if any link exists.
            pass
        # Many Moodle-like paging uses page=0 as first; but links show numbers. We'll attempt 0 then 1.
        candidate_starts = [0, 1]

        async def fetch_page(page_index: int) -> tuple[str, str]:
            if page_index in (0, 1):
                return first_url_path, first_html
            return f"/mod/courseboard/view.php?id={board_id}&page={page_index}", await _fetch_html(
                f"/mod/courseboard/view.php?id={board_id}&page={page_index}"
            )

        collected: list[dict[str, Any]] = []
        seen_bwids: set[str] = set()

        # Determine a reasonable sequence of pages to try:
        # - if pagination provides explicit pages, crawl them in increasing order
        # - else try [0,1] only.
        page_sequence: list[int]
        if pages:
            page_sequence = pages[:]
            # Also ensure 0 or 1 is included at the beginning for newest posts.
            if 0 not in page_sequence:
                page_sequence.insert(0, 0)
        else:
            page_sequence = candidate_starts

        pages_crawled = 0
        for page_index in page_sequence:
            if pages_crawled >= max_pages:
                break
            url_path, html = await fetch_page(page_index)
            soup = BeautifulSoup(html, "html.parser")

            # Look for a list table with post titles and dates.
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
                    for n in needles:
                        for i, h in enumerate(headers_norm):
                            if n in h:
                                return i
                    return None

                title_i = col_index("title", "제목", "subject") or 0
                date_i = col_index("date", "작성", "등록", "posted", "일자")

                rows = table.find_all("tr")
                if rows and rows[0].find_all("th"):
                    rows = rows[1:]

                for row in rows:
                    cells = row.find_all(["td", "th"])
                    if not cells or title_i >= len(cells):
                        continue
                    title_cell = cells[title_i]
                    a = title_cell.find("a", href=True)
                    title = _norm_text(title_cell.get_text(" ", strip=True))
                    href = a["href"] if a else None

                    posted_raw = None
                    if date_i is not None and date_i < len(cells):
                        posted_raw = _norm_text(cells[date_i].get_text(" ", strip=True)) or None

                    post_id = _extract_notice_id_from_href(href) if href else None
                    if post_id and post_id in seen_bwids:
                        continue
                    if post_id:
                        seen_bwids.add(post_id)

                    collected.append(
                        {
                            "board_id": str(board_id),
                            "id": post_id,
                            "title": title,
                            "url": _abs_url(config.base_url, href) if href else _abs_url(config.base_url, url_path),
                            "posted_raw": posted_raw,
                            "posted_iso": _parse_datetime_guess(posted_raw) if posted_raw else None,
                        }
                    )

                    if stop_bwid and post_id == stop_bwid:
                        break

                if stop_bwid and any(it.get("id") == stop_bwid for it in collected):
                    pages_crawled += 1
                    break

            else:
                # Generic fallback: grab links that look like post view pages.
                for a in soup.find_all("a", href=True):
                    href = a["href"]
                    if "mod/courseboard" not in href:
                        continue
                    title = _norm_text(a.get_text(" ", strip=True))
                    if not title:
                        continue
                    post_id = _extract_notice_id_from_href(href)
                    if post_id and post_id in seen_bwids:
                        continue
                    if post_id:
                        seen_bwids.add(post_id)
                    collected.append(
                        {
                            "board_id": str(board_id),
                            "id": post_id,
                            "title": title,
                            "url": _abs_url(config.base_url, href),
                            "posted_raw": None,
                            "posted_iso": None,
                        }
                    )
                    if stop_bwid and post_id == stop_bwid:
                        break

            pages_crawled += 1
            if stop_bwid and any(it.get("id") == stop_bwid for it in collected):
                break

        items.extend(collected)
        continue

    return items


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

    courses = await klms_list_courses(include_all=False)
    course_ids = [c["id"] for c in courses]

    # Assignments + materials per course.
    current_courses: dict[str, Any] = {}
    assignments_new: list[dict[str, Any]] = []
    assignments_updated: list[dict[str, Any]] = []
    materials_new: list[dict[str, Any]] = []

    for cid in course_ids:
        assignments = await klms_list_assignments(cid)
        materials = await klms_list_files(cid)
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
        discovered = []
        for c in courses:
            cid = c["id"]
            course_html = await _fetch_html(f"/course/view.php?id={cid}&section=0")
            for b in _discover_notice_board_ids_from_course_page(course_html):
                discovered.append(b["board_id"])
        board_ids = list(dict.fromkeys(discovered))

    notices_new: list[dict[str, Any]] = []
    current_boards: dict[str, Any] = {}
    for bid in board_ids:
        prev_board = snapshot.get("boards", {}).get(bid, {})
        prev_posts = prev_board.get("posts", {}) or {}
        # Use any previously-seen bwid as stop point to avoid paging forever.
        stop_bwid = None
        if prev_posts:
            # stop at the newest previously-seen post if we can infer it
            stop_bwid = next(iter(prev_posts.keys()))
        posts = await klms_list_notices(bid, max_pages=max_notice_pages, stop_bwid=stop_bwid)
        current_posts = {str(p.get("id")): p for p in posts if p.get("id")}
        current_boards[bid] = {"posts": current_posts}
        for pid, p in current_posts.items():
            if pid not in prev_posts:
                notices_new.append(p)

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
        course_ids = [c["id"] for c in await klms_list_courses()]
    if not course_ids:
        raise ValueError(f"Pass course_id or configure course_ids in {CONFIG_PATH}")

    items: list[dict[str, Any]] = []
    for cid in course_ids:
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

        items.extend(per_course_items)

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

    async with _authenticated_context(headless=True, accept_downloads=True) as (context, auth_mode):
        # Preflight auth check so we fail fast (rather than timing out waiting for a download).
        auth_page = await context.new_page()
        await auth_page.goto(
            _abs_url(config.base_url, config.dashboard_path),
            wait_until="domcontentloaded",
            timeout=15_000,
        )
        auth_html = await auth_page.content()
        auth_final_url = auth_page.url
        await auth_page.close()
        if _looks_logged_out(auth_html) or _looks_login_url(auth_final_url):
            _raise_auth_error(final_url=auth_final_url)

        page = await context.new_page()
        async with page.expect_download() as download_info:
            # Some KLMS resource URLs trigger downloads immediately, causing Playwright to raise:
            # "Page.goto: Download is starting". Treat that as success.
            try:
                await page.goto(resolved_url, wait_until="commit", timeout=30_000)
            except Exception as e:  # noqa: BLE001
                if "Download is starting" not in str(e):
                    raise
        download = await download_info.value

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
        "preferred_mode": "profile",
    }


if __name__ == "__main__":
    raise SystemExit("This module is intended to be used via `kaist klms ...`.")
