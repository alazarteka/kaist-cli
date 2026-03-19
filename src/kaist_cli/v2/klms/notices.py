from __future__ import annotations

import re
import sys
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup  # type: ignore[import-untyped]

from ..contracts import CommandError, CommandResult
from .auth import AuthService, looks_logged_out_html, looks_login_url
from .cache import list_cache_entries, load_cache_entry, save_cache_value
from .assignments import _attachment_filename_from_url, _looks_like_attachment_url, _parse_datetime_guess
from .config import KlmsConfig, abs_url, load_config
from .courses import _course_code_base, _norm_text, _select_dashboard_courses
from .deadline import RefreshDeadline
from .models import Notice
from .paths import KlmsPaths
from .provider_state import ProviderLoad
from .session import KlmsSessionBootstrap, build_session_bootstrap, fetch_html_batch
from .validate import looks_klms_error_html

NOTICE_BOARD_TTL_SECONDS = 6 * 3600
NOTICE_LIST_TTL_SECONDS = 5 * 60
MAX_NOTICE_HTTP_WORKERS = 4


def _emit_pull_progress(index: int, total: int, title: str, *, status: str | None = None, detail: str | None = None) -> None:
    prefix = f"[{index}/{total}]"
    safe_title = " ".join(str(title or "unnamed attachment").split()) or "unnamed attachment"
    if status is None:
        message = f"{prefix} downloading {safe_title} ..."
    else:
        message = f"{prefix} {status} {safe_title}"
        if detail:
            message += f" ({detail})"
    print(message, file=sys.stderr, flush=True)


def _extract_course_ids_from_dashboard(
    html: str,
    *,
    base_url: str,
    configured_ids: tuple[str, ...],
    exclude_patterns: tuple[str, ...],
    course_query: str | None = None,
    course_id: str | None = None,
) -> list[str]:
    if course_id:
        target = str(course_id).strip()
        out = [target] if target else []
    else:
        discovered = _select_dashboard_courses(
            html,
            base_url=base_url,
            exclude_patterns=exclude_patterns,
            course_query=course_query,
            include_past=False,
            allow_termless_fallback=True,
        )
        out = [str(course.id).strip() for course in discovered if str(course.id).strip()]
    if not course_id and not course_query:
        out.extend(str(configured_id).strip() for configured_id in configured_ids if str(configured_id).strip())
    seen: set[str] = set()
    deduped: list[str] = []
    for value in out:
        if not value or value in seen:
            continue
        seen.add(value)
        deduped.append(value)
    return deduped


def _course_meta_map_from_dashboard(
    html: str,
    *,
    base_url: str,
    exclude_patterns: tuple[str, ...],
    course_query: str | None = None,
    course_id: str | None = None,
) -> dict[str, dict[str, str | None]]:
    selected = _select_dashboard_courses(
        html,
        base_url=base_url,
        exclude_patterns=exclude_patterns,
        course_query=course_query,
        include_past=False,
        allow_termless_fallback=True,
    )
    out = {
        str(course.id): {
            "course_id": str(course.id),
            "course_title": course.title,
            "course_code": course.course_code,
            "course_code_base": _course_code_base(course.course_code),
        }
        for course in selected
        if str(course.id).strip()
    }
    if course_id:
        target = str(course_id).strip()
        if target and target not in out:
            out[target] = {
                "course_id": target,
                "course_title": None,
                "course_code": None,
                "course_code_base": None,
            }
    return out


def _discover_notice_board_ids_from_course_page(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    found: list[str] = []

    def in_header_like_region(element: Any) -> bool:
        current = element
        for _ in range(12):
            if not current or not getattr(current, "attrs", None):
                break
            classes = " ".join(current.attrs.get("class", [])).lower()
            if any(marker in classes for marker in ("ks-header", "all-menu", "tooltip-layer", "breadcrumb", "navbar", "footer", "menu")):
                return True
            current = current.parent
        return False

    for anchor in soup.find_all("a", href=True):
        href = str(anchor["href"])
        if "mod/courseboard/view.php" not in href:
            continue
        match = re.search(r"[?&]id=(\d+)", href)
        if not match:
            continue
        board_id = match.group(1)
        label = _norm_text(anchor.get_text(" ", strip=True)).lower()
        if anchor.get("target") == "_blank" or in_header_like_region(anchor):
            continue
        if board_id in {"32044", "32045", "32047", "531193"}:
            continue
        if label in {"notice", "guide to klms", "q&a", "faq"}:
            continue
        found.append(board_id)
    return list(dict.fromkeys(found))


def _extract_pagination_pages(soup: BeautifulSoup) -> list[int]:
    pages: set[int] = set()
    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href") or "")
        for pattern in (r"[?&]page=(\d+)", r"[?&]p=(\d+)"):
            match = re.search(pattern, href)
            if match:
                pages.add(int(match.group(1)))
                break
    return sorted(pages)


def _plan_notice_page_sequence(first_soup: BeautifulSoup, *, max_pages: int) -> tuple[int, list[int]]:
    pages = [page for page in _extract_pagination_pages(first_soup) if page >= 0]
    if pages:
        first_page_index = 0 if 0 in pages else min(pages)
        sequence = [first_page_index] + [page for page in pages if page != first_page_index]
    else:
        first_page_index = 0
        sequence = [0, 1]
    return first_page_index, sequence[: max(0, max_pages)]


def _extract_notice_id_from_href(href: str | None) -> str | None:
    if not href:
        return None
    match = re.search(r"[?&]bwid=(\d+)", href)
    if match:
        return match.group(1)
    match = re.search(r"[?&]id=(\d+)", href)
    return match.group(1) if match else None


def _looks_like_hidden_notice(title: str) -> bool:
    text = _norm_text(title).lower()
    if not text:
        return True
    markers = (
        "this is a hidden post",
        "hidden post",
        "비밀글",
        "숨김글",
    )
    return any(marker in text for marker in markers)


def _parse_notice_items_from_soup(
    soup: BeautifulSoup,
    *,
    board_id: str,
    base_url: str,
    fallback_url_path: str,
) -> list[Notice]:
    def find_table_with_title_headers() -> tuple[list[str], Any] | None:
        for table in soup.find_all("table"):
            rows = table.find_all("tr")
            if not rows:
                continue
            headers = [_norm_text(cell.get_text(" ", strip=True)) for cell in rows[0].find_all(["th", "td"])]
            headers_norm = [header.lower() for header in headers]
            if any(any(needle in header for needle in ("title", "제목", "subject")) for header in headers_norm):
                return headers, table
        return None

    found = find_table_with_title_headers()
    if found:
        headers, table = found
        headers_norm = [header.lower() for header in headers]

        def col_index(*needles: str) -> int | None:
            for needle in needles:
                for index, header in enumerate(headers_norm):
                    if needle in header:
                        return index
            return None

        title_i = col_index("title", "제목", "subject") or 0
        date_i = col_index("date", "작성", "등록", "posted", "일자")
        rows = table.find_all("tr")
        if rows and rows[0].find_all("th"):
            rows = rows[1:]

        notices: list[Notice] = []
        for row in rows:
            cells = row.find_all(["td", "th"])
            if not cells or title_i >= len(cells):
                continue
            title_cell = cells[title_i]
            link = title_cell.find("a", href=True)
            title = _norm_text(title_cell.get_text(" ", strip=True))
            href = str(link["href"]) if link else None
            if _looks_like_hidden_notice(title):
                continue
            posted_raw = None
            if date_i is not None and date_i < len(cells):
                posted_raw = _norm_text(cells[date_i].get_text(" ", strip=True)) or None
            notice_id = _extract_notice_id_from_href(href)
            if href and "article.php" in href and notice_id is None:
                continue
            notices.append(
                Notice(
                    board_id=board_id,
                    id=notice_id,
                    title=title or "notice",
                    url=abs_url(base_url, href) if href else abs_url(base_url, fallback_url_path),
                    posted_raw=posted_raw,
                    posted_iso=_parse_datetime_guess(posted_raw) if posted_raw else None,
                    source="html:courseboard",
                    confidence=0.66,
                )
            )
        return notices

    out: list[Notice] = []
    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href") or "")
        if "mod/courseboard" not in href:
            continue
        title = _norm_text(anchor.get_text(" ", strip=True))
        if not title or _looks_like_hidden_notice(title):
            continue
        notice_id = _extract_notice_id_from_href(href)
        if "article.php" not in href or notice_id is None:
            continue
        out.append(
            Notice(
                board_id=board_id,
                id=notice_id,
                title=title,
                url=abs_url(base_url, href),
                posted_raw=None,
                posted_iso=None,
                source="html:courseboard-fallback",
                confidence=0.58,
            )
        )
    return out


def _extract_notice_ids_from_url(url: str | None) -> tuple[str | None, str | None]:
    if not url:
        return None, None
    parsed = urlparse(url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    board_id = (query.get("id") or [None])[0]
    notice_id = (query.get("bwid") or [None])[0]
    return (str(board_id).strip() or None) if isinstance(board_id, str) else None, (str(notice_id).strip() or None) if isinstance(notice_id, str) else None


def _extract_notice_title_from_soup(soup: BeautifulSoup) -> str | None:
    for selector in ("h1", "h2", "#page-header h1", ".subject", ".board-title", ".article-title", ".post-title"):
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
        if title and ":" in title:
            left = _norm_text(title.split(":", 1)[0])
            if left:
                return left
        return title or None
    return None


def _extract_notice_meta_from_soup(soup: BeautifulSoup) -> tuple[str | None, str | None]:
    author = None
    posted_raw = None
    label_patterns = {
        "author": re.compile(r"^(?:author|writer|작성자|등록자)\s*[:：]?\s*(.+)$", flags=re.IGNORECASE),
        "posted": re.compile(r"^(?:wrote on|date|posted|작성일|등록일)\s*[:：]?\s*(.+)$", flags=re.IGNORECASE),
    }

    for selector, key in (
        (".courseboard_view .info .writer", "author"),
        (".courseboard_view .info .date", "posted"),
        (".courseboard_view .info .regdate", "posted"),
    ):
        node = soup.select_one(selector)
        if not node:
            continue
        text = _norm_text(node.get_text(" ", strip=True))
        if not text:
            continue
        match = label_patterns[key].match(text)
        value = _norm_text(match.group(1) if match else text)
        if key == "author" and not author and value:
            author = value
        if key == "posted" and not posted_raw and value:
            posted_raw = value

    for row in soup.select("table tr"):
        th = row.find("th")
        td = row.find("td")
        if not th or not td:
            continue
        key = _norm_text(th.get_text(" ", strip=True)).lower()
        value = _norm_text(td.get_text(" ", strip=True))
        if not value:
            continue
        if not author and any(token in key for token in ("작성자", "author", "writer", "등록자")):
            author = value
        if not posted_raw and any(token in key for token in ("작성일", "등록일", "date", "posted", "시간")):
            posted_raw = value

    if not author or not posted_raw:
        for node in soup.select(".courseboard_view .info .author, .courseboard_view .info .writer, .courseboard_view .info .user, .courseboard_view .info .posted, .courseboard_view .info .date, .courseboard_view .info .time, .courseboard_view .info .regdate, .courseboard_view .info, .post-info"):
            text = _norm_text(node.get_text(" ", strip=True))
            if not text:
                continue
            if not author:
                match = re.search(r"(?:작성자|author|writer)\s*[:：]\s*(.+?)(?:\s+(?:wrote on|작성일|date|posted)\s*[:：].+)?$", text, flags=re.IGNORECASE)
                if match:
                    author = _norm_text(match.group(1))
            if not posted_raw:
                match = re.search(r"(?:wrote on|작성일|등록일|date|posted)\s*[:：]\s*(.+?)(?:\s+(?:views|조회)\s*[:：].+)?$", text, flags=re.IGNORECASE)
                if match:
                    posted_raw = _norm_text(match.group(1))
    return author, posted_raw


def _select_notice_body_node(soup: BeautifulSoup) -> Any:
    selectors = (
        (".courseboard_view .content", True),
        (".courseboard .content", True),
        (".courseboard_view .text_to_html", True),
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
    )
    best = None
    best_score = -1
    normalized_selectors: list[tuple[str, bool]] = []
    for selector in selectors:
        if isinstance(selector, tuple):
            normalized_selectors.append(selector)
        else:
            normalized_selectors.append((selector, False))

    for selector, prefer_first in normalized_selectors:
        for node in soup.select(selector):
            text = _norm_text(node.get_text(" ", strip=True))
            if len(text) < 40:
                continue
            if prefer_first:
                return node
            score = len(text)
            if node.find("p"):
                score += 40
            if len(node.find_all("a", href=True)) > 30:
                score -= 300
            if score > best_score:
                best = node
                best_score = score
    return best or soup.body or soup


def _sanitize_notice_body_node(body_node: Any) -> tuple[str | None, str | None]:
    fragment = BeautifulSoup(str(body_node), "html.parser")
    for selector in (
        ".pre_next",
        ".button_area",
        ".modal",
        ".mod-tabmenus-wrap",
        ".activity-navigation",
        ".info",
        ".subject",
        "form",
        "button",
    ):
        for node in fragment.select(selector):
            node.decompose()
    root = fragment.body or fragment
    body_text = _norm_text(root.get_text("\n", strip=True)) or None
    body_html = str(root) if body_text else None
    return body_text, body_html


def _collect_notice_attachments(soup: BeautifulSoup, *, base_url: str) -> tuple[dict[str, Any], ...]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for link in soup.find_all("a", href=True):
        href = str(link.get("href") or "").strip()
        if not href or href.startswith("#") or href.lower().startswith("javascript:"):
            continue
        url = abs_url(base_url, href)
        classes = " ".join(str(item) for item in (link.get("class") or [])).lower()
        if not _looks_like_attachment_url(url) and not any(token in classes for token in ("attach", "download", "file")):
            continue
        if url in seen:
            continue
        seen.add(url)
        title = _norm_text(link.get_text(" ", strip=True)) or _attachment_filename_from_url(url) or url
        out.append(
            {
                "title": title,
                "url": url,
                "filename": _attachment_filename_from_url(url),
            }
        )
    return tuple(out)


def _iso_from_epoch_seconds(value: float | int | None) -> str | None:
    if not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    except Exception:
        return None


def _cache_is_fresh_enough(entry: dict[str, Any] | None, *, max_age_seconds: int = 3600) -> bool:
    if not isinstance(entry, dict):
        return False
    age_seconds = entry.get("age_seconds")
    if isinstance(age_seconds, (int, float)):
        return float(age_seconds) <= float(max_age_seconds)
    stored_at = entry.get("stored_at")
    if isinstance(stored_at, (int, float)):
        return (datetime.now(timezone.utc).timestamp() - float(stored_at)) <= float(max_age_seconds)
    return False


def _finalize_notice_items(items: list[Notice], *, since_iso: str | None, limit: int | None) -> list[Notice]:
    filtered = items
    if since_iso:
        floor = str(since_iso).strip()
        filtered = [item for item in filtered if item.posted_iso and item.posted_iso >= floor]
    filtered = sorted(filtered, key=lambda item: (item.posted_iso is not None, item.posted_iso or "", item.title), reverse=True)
    if limit is not None:
        filtered = filtered[: max(0, limit)]
    return filtered


def _matching_notice_count(items: list[Notice], *, since_iso: str | None) -> int:
    if not since_iso:
        return len(items)
    floor = str(since_iso).strip()
    return sum(1 for item in items if item.posted_iso and item.posted_iso >= floor)


def _provider_warning(code: str, message: str, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"code": code, "message": message}
    payload.update(extra)
    return payload


def _notice_detail_target(notice: Notice, *, base_url: str) -> str | None:
    if notice.url:
        return str(notice.url)
    if notice.board_id and notice.id:
        return abs_url(base_url, f"/mod/courseboard/article.php?id={notice.board_id}&bwid={notice.id}")
    return None


def _merge_notice_rows(list_row: Notice, detail_row: Notice, *, auth_mode: str) -> Notice:
    return Notice(
        board_id=detail_row.board_id or list_row.board_id,
        id=detail_row.id or list_row.id,
        title=detail_row.title or list_row.title,
        url=detail_row.url or list_row.url,
        posted_raw=detail_row.posted_raw or list_row.posted_raw,
        posted_iso=detail_row.posted_iso or list_row.posted_iso,
        author=detail_row.author or list_row.author,
        body_text=detail_row.body_text or list_row.body_text,
        body_html=detail_row.body_html or list_row.body_html,
        attachments=detail_row.attachments or list_row.attachments,
        detail_available=bool(detail_row.detail_available or list_row.detail_available),
        source=detail_row.source or list_row.source,
        confidence=max(float(detail_row.confidence or 0.0), float(list_row.confidence or 0.0)),
        auth_mode=auth_mode or detail_row.auth_mode or list_row.auth_mode,
    )


def _enrich_notice_items_from_detail(
    items: list[Notice],
    *,
    base_url: str,
    auth_mode: str,
    bootstrap: KlmsSessionBootstrap,
    deadline: RefreshDeadline | None,
) -> list[Notice]:
    targets: dict[str, Notice] = {}
    for notice in items:
        target = _notice_detail_target(notice, base_url=base_url)
        if target:
            targets[target] = notice
    if not targets:
        return items

    responses = fetch_html_batch(
        bootstrap.http,
        list(targets.keys()),
        deadline=deadline,
        max_workers=MAX_NOTICE_HTTP_WORKERS,
    )
    enriched: list[Notice] = []
    for notice in items:
        target = _notice_detail_target(notice, base_url=base_url)
        response = responses.get(target or "")
        if response is None or looks_login_url(response.url) or looks_logged_out_html(response.text) or looks_klms_error_html(response.text):
            enriched.append(notice)
            continue
        detail = _parse_notice_detail_from_html(
            response.text,
            base_url=base_url,
            url=response.url,
            fallback_board_id=notice.board_id,
            fallback_notice_id=notice.id,
            include_html=False,
            auth_mode=auth_mode,
        )
        enriched.append(_merge_notice_rows(notice, detail, auth_mode=auth_mode))
    return enriched


def _parse_notice_detail_from_html(
    html: str,
    *,
    base_url: str,
    url: str | None = None,
    fallback_board_id: str | None = None,
    fallback_notice_id: str | None = None,
    include_html: bool = False,
    auth_mode: str | None = None,
) -> Notice:
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
            board_candidate, notice_candidate = _extract_notice_ids_from_url(href)
            board_id = board_id or board_candidate
            notice_id = notice_id or notice_candidate
            if board_id and notice_id:
                break

    title = _extract_notice_title_from_soup(soup) or (f"notice-{notice_id}" if notice_id else "notice")
    author, posted_raw = _extract_notice_meta_from_soup(soup)
    posted_iso = _parse_datetime_guess(posted_raw) if posted_raw else None
    body_node = _select_notice_body_node(soup)
    body_text, body_html = _sanitize_notice_body_node(body_node)

    return Notice(
        board_id=board_id,
        id=notice_id,
        title=title,
        url=url,
        posted_raw=posted_raw,
        posted_iso=posted_iso,
        author=author,
        body_text=body_text,
        body_html=body_html if include_html else None,
        attachments=_collect_notice_attachments(soup, base_url=base_url),
        detail_available=bool(body_text),
        source="html:courseboard-article",
        confidence=0.78 if body_text else 0.62,
        auth_mode=auth_mode,
    )


class NoticeService:
    def __init__(self, paths: KlmsPaths, auth: AuthService) -> None:
        self._paths = paths
        self._auth = auth

    @staticmethod
    def _notice_board_cache_key(config: KlmsConfig, course_ids: list[str]) -> str:
        return "::".join(
            [
                "notice-board-map-v2",
                config.base_url.rstrip("/"),
                config.dashboard_path,
                ",".join(course_ids),
            ]
        )

    @staticmethod
    def _fallback_notice_board_ids_from_cache(paths: KlmsPaths, config: KlmsConfig) -> list[str]:
        prefix = f"notice-board-map-v2::{config.base_url.rstrip('/')}::{config.dashboard_path}::"
        candidates = list_cache_entries(paths, prefixes=(prefix,))
        if not candidates:
            return []
        newest = sorted(candidates.values(), key=lambda entry: float(entry.get("stored_at") or 0.0), reverse=True)
        ordered_keys = [
            key
            for key, _entry in sorted(
                candidates.items(),
                key=lambda item: float(item[1].get("stored_at") or 0.0),
                reverse=True,
            )
        ]
        for cache_key, entry in zip(ordered_keys, newest, strict=False):
            value = entry.get("value")
            if not isinstance(value, dict):
                continue
            course_order = [segment.strip() for segment in str(cache_key).split("::")[-1].split(",") if segment.strip()]
            board_ids: list[str] = []
            for course_id in course_order:
                rows = value.get(course_id)
                if isinstance(rows, list):
                    board_ids.extend(str(board_id).strip() for board_id in rows if str(board_id).strip())
            for course_id, rows in value.items():
                if course_id in course_order or not isinstance(rows, list):
                    continue
                if isinstance(rows, list):
                    board_ids.extend(str(board_id).strip() for board_id in rows if str(board_id).strip())
            if board_ids:
                return list(dict.fromkeys(board_ids))
        return []

    @staticmethod
    def _notice_list_cache_key(config: KlmsConfig, board_ids: list[str], max_pages: int) -> str:
        return "::".join(
            [
                "notice-list-v2",
                config.base_url.rstrip("/"),
                str(max_pages),
                ",".join(board_ids),
            ]
        )

    def _load_notice_cache_entry(
        self,
        *,
        config: KlmsConfig,
        board_ids: list[str],
        max_pages: int,
    ) -> dict[str, Any] | None:
        exact_key = self._notice_list_cache_key(config, board_ids, max_pages)
        exact = load_cache_entry(self._paths, exact_key)
        if exact is not None:
            return exact

        prefix = f"notice-list-v2::{config.base_url.rstrip('/')}::"
        suffix = f"::{','.join(board_ids)}"
        candidates = list_cache_entries(self._paths, prefixes=(prefix,))
        matches: list[tuple[float, dict[str, Any]]] = []
        for key, entry in candidates.items():
            if not str(key).endswith(suffix):
                continue
            stored_at = float(entry.get("stored_at") or 0.0)
            matches.append((stored_at, entry))
        if not matches:
            return None
        matches.sort(key=lambda item: (bool(item[1].get("stale")), -item[0]))
        return matches[0][1]

    def list_with_context(
        self,
        *,
        context: Any,
        config: KlmsConfig,
        auth_mode: str,
        notice_board_id: str | None = None,
        course_id: str | None = None,
        course_query: str | None = None,
        max_pages: int = 1,
        since_iso: str | None = None,
        limit: int | None = None,
        bootstrap: KlmsSessionBootstrap | None = None,
    ) -> CommandResult:
        bootstrap = bootstrap or build_session_bootstrap(
            self._paths,
            context=context,
            config=config,
            auth_mode=auth_mode,
        )
        notices = self._list_html(
            context=context,
            config=config,
            auth_mode=auth_mode,
            notice_board_id=notice_board_id,
            course_id=course_id,
            course_query=course_query,
            max_pages=max_pages,
            since_iso=since_iso,
            limit=limit,
            bootstrap=bootstrap,
        )
        return CommandResult(data=[notice.to_dict() for notice in notices], source="html", capability="partial")

    def load_for_dashboard(
        self,
        *,
        context: Any,
        config: KlmsConfig,
        auth_mode: str,
        notice_board_id: str | None = None,
        course_id: str | None = None,
        course_query: str | None = None,
        max_pages: int = 1,
        since_iso: str | None = None,
        limit: int | None = None,
        bootstrap: KlmsSessionBootstrap | None = None,
        deadline: RefreshDeadline | None = None,
        prefer_cache: bool = True,
    ) -> ProviderLoad:
        bootstrap = bootstrap or build_session_bootstrap(
            self._paths,
            context=context,
            config=config,
            auth_mode=auth_mode,
        )
        board_ids = self._resolve_notice_board_ids(
            context=context,
            config=config,
            explicit_board_id=notice_board_id,
            course_id=course_id,
            course_query=course_query,
            bootstrap=bootstrap,
            deadline=deadline,
            allow_stale_cache=True,
        )
        if not board_ids:
            return ProviderLoad(
                items=[],
                source="html",
                capability="degraded",
                freshness_mode="live",
                cache_hit=False,
                stale=False,
                fetched_at=None,
                expires_at=None,
                refresh_attempted=False,
                ok=False,
                warnings=(
                    _provider_warning(
                        "LIVE_REFRESH_FAILED",
                        "No notice boards could be discovered for this session.",
                    ),
                ),
            )

        cache_key = self._notice_list_cache_key(config, board_ids, max_pages)
        cache_entry = self._load_notice_cache_entry(config=config, board_ids=board_ids, max_pages=max_pages)
        cached_rows = cache_entry.get("value") if isinstance(cache_entry, dict) else None
        cached_items = [Notice(**row) for row in cached_rows if isinstance(row, dict)] if isinstance(cached_rows, list) else []
        cached_filtered = _finalize_notice_items(cached_items, since_iso=since_iso, limit=limit)
        cache_fresh_enough = _cache_is_fresh_enough(cache_entry)
        if prefer_cache and cache_entry is not None and (not bool(cache_entry.get("stale")) or cache_fresh_enough):
            return ProviderLoad(
                items=[item.to_dict() for item in cached_filtered],
                source="html",
                capability="partial",
                freshness_mode="cache",
                cache_hit=True,
                stale=bool(cache_entry.get("stale")),
                fetched_at=_iso_from_epoch_seconds(cache_entry.get("stored_at")),
                expires_at=_iso_from_epoch_seconds(cache_entry.get("expires_at")),
                refresh_attempted=False,
                ok=True,
            )

        if deadline is not None and deadline.hard_expired():
            if cache_entry is not None and cached_filtered:
                warnings: list[dict[str, Any]] = []
                if not _cache_is_fresh_enough(cache_entry):
                    warnings.append(_provider_warning("LIVE_REFRESH_TIMEOUT", "Interactive refresh budget expired before notice refresh completed."))
                if bool(cache_entry.get("stale")):
                    warnings.insert(0, _provider_warning("STALE_CACHE", "Returning stale notice cache because live refresh could not finish in time."))
                return ProviderLoad(
                    items=[item.to_dict() for item in cached_filtered],
                    source="html",
                    capability="partial",
                    freshness_mode="cache",
                    cache_hit=True,
                    stale=bool(cache_entry.get("stale")),
                    fetched_at=_iso_from_epoch_seconds(cache_entry.get("stored_at")),
                    expires_at=_iso_from_epoch_seconds(cache_entry.get("expires_at")),
                    refresh_attempted=True,
                    ok=True,
                    warnings=tuple(warnings),
                )
            return ProviderLoad(
                items=[],
                source="html",
                capability="degraded",
                freshness_mode="live",
                cache_hit=False,
                stale=False,
                fetched_at=None,
                expires_at=None,
                refresh_attempted=False,
                ok=False,
                warnings=(
                    _provider_warning("LIVE_REFRESH_TIMEOUT", "Interactive refresh budget expired before notice refresh started."),
                ),
            )

        try:
            live_items = self._refresh_notice_items(
                config=config,
                auth_mode=auth_mode,
                board_ids=board_ids,
                max_pages=max_pages,
                since_iso=since_iso,
                limit=limit,
                bootstrap=bootstrap,
                deadline=deadline,
            )
        except TimeoutError:
            if cache_entry is not None and cached_filtered:
                warnings: list[dict[str, Any]] = []
                if not _cache_is_fresh_enough(cache_entry):
                    warnings.append(_provider_warning("LIVE_REFRESH_TIMEOUT", "Notice refresh exceeded the interactive deadline."))
                if bool(cache_entry.get("stale")):
                    warnings.insert(0, _provider_warning("STALE_CACHE", "Returning stale notice cache because live refresh timed out."))
                return ProviderLoad(
                    items=[item.to_dict() for item in cached_filtered],
                    source="html",
                    capability="partial",
                    freshness_mode="cache",
                    cache_hit=True,
                    stale=bool(cache_entry.get("stale")),
                    fetched_at=_iso_from_epoch_seconds(cache_entry.get("stored_at")),
                    expires_at=_iso_from_epoch_seconds(cache_entry.get("expires_at")),
                    refresh_attempted=True,
                    ok=True,
                    warnings=tuple(warnings),
                )
            return ProviderLoad(
                items=[],
                source="html",
                capability="degraded",
                freshness_mode="live",
                cache_hit=False,
                stale=False,
                fetched_at=None,
                expires_at=None,
                refresh_attempted=True,
                ok=False,
                warnings=(
                    _provider_warning("LIVE_REFRESH_TIMEOUT", "Notice refresh exceeded the interactive deadline."),
                ),
            )
        except CommandError:
            raise
        except Exception as exc:
            if cache_entry is not None and cached_filtered:
                warnings: list[dict[str, Any]] = []
                if not _cache_is_fresh_enough(cache_entry):
                    warnings.append(_provider_warning("LIVE_REFRESH_FAILED", "Notice refresh failed; returning cached notice data.", error=str(exc)))
                if bool(cache_entry.get("stale")):
                    warnings.insert(0, _provider_warning("STALE_CACHE", "Returning stale notice cache because live refresh failed."))
                return ProviderLoad(
                    items=[item.to_dict() for item in cached_filtered],
                    source="html",
                    capability="partial",
                    freshness_mode="cache",
                    cache_hit=True,
                    stale=bool(cache_entry.get("stale")),
                    fetched_at=_iso_from_epoch_seconds(cache_entry.get("stored_at")),
                    expires_at=_iso_from_epoch_seconds(cache_entry.get("expires_at")),
                    refresh_attempted=True,
                    ok=True,
                    warnings=tuple(warnings),
                )
            return ProviderLoad(
                items=[],
                source="html",
                capability="degraded",
                freshness_mode="live",
                cache_hit=False,
                stale=False,
                fetched_at=None,
                expires_at=None,
                refresh_attempted=True,
                ok=False,
                warnings=(
                    _provider_warning("LIVE_REFRESH_FAILED", "Notice refresh failed.", error=str(exc)),
                ),
            )

        live_filtered = _finalize_notice_items(live_items, since_iso=since_iso, limit=limit)
        fresh_entry = load_cache_entry(self._paths, cache_key)
        return ProviderLoad(
            items=[item.to_dict() for item in live_filtered],
            source="html",
            capability="partial",
            freshness_mode="live",
            cache_hit=False,
            stale=False,
            fetched_at=_iso_from_epoch_seconds((fresh_entry or {}).get("stored_at")),
            expires_at=_iso_from_epoch_seconds((fresh_entry or {}).get("expires_at")),
            refresh_attempted=True,
            ok=True,
        )

    def refresh_cache_with_context(
        self,
        *,
        context: Any,
        config: KlmsConfig,
        auth_mode: str,
        max_pages: int = 1,
        course_id: str | None = None,
        bootstrap: KlmsSessionBootstrap | None = None,
    ) -> ProviderLoad:
        return self.load_for_dashboard(
            context=context,
            config=config,
            auth_mode=auth_mode,
            course_id=course_id,
            max_pages=max_pages,
            bootstrap=bootstrap,
            deadline=None,
            prefer_cache=False,
        )

    def list(
        self,
        *,
        notice_board_id: str | None = None,
        course_id: str | None = None,
        course_query: str | None = None,
        max_pages: int = 1,
        since_iso: str | None = None,
        limit: int | None = None,
    ) -> CommandResult:
        config = load_config(self._paths)
        max_pages = max(1, min(max_pages, 10))

        def callback(context: Any, auth_mode: str) -> CommandResult:
            return self.list_with_context(
                context=context,
                config=config,
                auth_mode=auth_mode,
                notice_board_id=notice_board_id,
                course_id=course_id,
                course_query=course_query,
                max_pages=max_pages,
                since_iso=since_iso,
                limit=limit,
            )

        return self._auth.run_authenticated(
            config=config,
            headless=True,
            accept_downloads=False,
            timeout_seconds=10.0,
            callback=callback,
        )

    def show(
        self,
        notice_id: str,
        *,
        notice_board_id: str | None = None,
        course_id: str | None = None,
        course_query: str | None = None,
        max_pages: int = 3,
        include_html: bool = False,
    ) -> CommandResult:
        config = load_config(self._paths)
        target_notice_id = str(notice_id).strip()
        if not target_notice_id:
            raise CommandError(code="CONFIG_INVALID", message="Notice ID is required.", exit_code=40)

        def callback(context: Any, auth_mode: str) -> CommandResult:
            bootstrap = build_session_bootstrap(
                self._paths,
                context=context,
                config=config,
                auth_mode=auth_mode,
            )
            board_ids = self._resolve_notice_board_ids(
                context=context,
                config=config,
                explicit_board_id=notice_board_id,
                course_id=course_id,
                course_query=course_query,
                bootstrap=bootstrap,
            )
            if not board_ids:
                raise CommandError(
                    code="CONFIG_INVALID",
                    message="No notice boards found.",
                    hint="Configure notice_board_ids or let v2 discover boards from your course pages.",
                    exit_code=40,
                )

            metadata_row = None
            if notice_board_id is None:
                rows = self._list_html(
                    context=context,
                    config=config,
                    auth_mode=auth_mode,
                    notice_board_id=None,
                    course_id=course_id,
                    course_query=course_query,
                    max_pages=max_pages,
                    since_iso=None,
                    limit=None,
                    bootstrap=bootstrap,
                )
                for row in rows:
                    if str(row.id or "") == target_notice_id:
                        metadata_row = row
                        break

            candidates: list[tuple[str, str, bool]] = []
            if metadata_row and metadata_row.board_id and metadata_row.url:
                ordered = [metadata_row.board_id] + [board_id for board_id in board_ids if board_id != metadata_row.board_id]
                board_ids = ordered
                candidates.append((str(metadata_row.url), str(metadata_row.board_id), False))

            for board_id in board_ids:
                candidates.append((abs_url(config.base_url, f"/mod/courseboard/article.php?id={board_id}&bwid={target_notice_id}"), board_id, True))

            seen_urls: set[str] = set()
            for url, board_id, strict in candidates:
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                page = context.new_page()
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                    html = page.content()
                    final_url = page.url
                except Exception:
                    continue
                finally:
                    try:
                        page.close()
                    except Exception:
                        pass

                if strict:
                    lowered = final_url.lower()
                    if "mod/courseboard/article.php" not in lowered or f"bwid={target_notice_id}" not in lowered:
                        continue
                if looks_klms_error_html(html):
                    continue

                detail = _parse_notice_detail_from_html(
                    html,
                    base_url=config.base_url,
                    url=final_url,
                    fallback_board_id=board_id,
                    fallback_notice_id=target_notice_id,
                    include_html=include_html,
                    auth_mode=auth_mode,
                )
                if detail.id and detail.id != target_notice_id:
                    continue
                detail = Notice(**{**detail.to_dict(), "id": target_notice_id})
                if metadata_row:
                    merged = metadata_row.to_dict()
                    merged.update(detail.to_dict())
                    detail = Notice(
                        board_id=merged.get("board_id"),
                        id=merged.get("id"),
                        title=merged.get("title") or "notice",
                        url=merged.get("url"),
                        posted_raw=merged.get("posted_raw"),
                        posted_iso=merged.get("posted_iso"),
                        author=merged.get("author"),
                        body_text=merged.get("body_text"),
                        body_html=merged.get("body_html"),
                        attachments=tuple(merged.get("attachments") or ()),
                        detail_available=bool(merged.get("detail_available")),
                        source=str(merged.get("source") or "html:courseboard-article"),
                        confidence=float(merged.get("confidence") or 0.0),
                        auth_mode=merged.get("auth_mode"),
                    )
                return CommandResult(data=detail.to_dict(), source="html", capability="partial")

            raise CommandError(code="NOT_FOUND", message=f"Notice not found: {notice_id}", exit_code=44)

        return self._auth.run_authenticated(
            config=config,
            headless=True,
            accept_downloads=False,
            timeout_seconds=10.0,
            callback=callback,
        )

    def pull_attachments(
        self,
        *,
        course_id: str | None = None,
        course_query: str | None = None,
        since_iso: str | None = None,
        limit: int | None = None,
        subdir: str | None = None,
        dest: str | None = None,
        if_exists: str = "skip",
    ) -> CommandResult:
        from .files import FileService, _pull_subdir_for_item, _resolve_destination_root
        from .models import FileItem

        if subdir and dest:
            raise CommandError(
                code="CONFIG_INVALID",
                message="--subdir and --dest cannot be used together.",
                hint="Use --dest for an explicit directory, or --subdir for a path under the managed files root.",
                exit_code=40,
            )
        config = load_config(self._paths)
        if if_exists not in {"skip", "overwrite"}:
            raise CommandError(code="CONFIG_INVALID", message="if_exists must be 'skip' or 'overwrite'.", exit_code=40)

        def callback(context: Any, auth_mode: str) -> CommandResult:
            bootstrap = build_session_bootstrap(
                self._paths,
                context=context,
                config=config,
                auth_mode=auth_mode,
            )
            course_meta = _course_meta_map_from_dashboard(
                bootstrap.dashboard_html,
                base_url=config.base_url,
                exclude_patterns=config.exclude_course_title_patterns,
                course_query=course_query,
                course_id=course_id,
            )
            board_map = self._resolve_notice_board_map(
                context=context,
                config=config,
                explicit_board_id=None,
                course_id=course_id,
                course_query=course_query,
                bootstrap=bootstrap,
                allow_stale_cache=True,
            )
            board_to_course: dict[str, str] = {}
            for mapped_course_id, board_ids in board_map.items():
                course_id_text = str(mapped_course_id).strip()
                if not course_id_text:
                    continue
                for board_id_text in board_ids:
                    board_to_course[str(board_id_text).strip()] = course_id_text

            notices = self._list_html(
                context=context,
                config=config,
                auth_mode=auth_mode,
                notice_board_id=None,
                course_id=course_id,
                course_query=course_query,
                max_pages=3,
                since_iso=since_iso,
                limit=limit,
                bootstrap=bootstrap,
            )
            attachment_course_ids = {
                str(board_to_course.get(str(notice.board_id or "").strip()) or "").strip()
                for notice in notices
                for attachment in notice.attachments
                if str(attachment.get("url") or "").strip()
            }
            attachment_course_ids.discard("")
            include_course_dirs = len(attachment_course_ids) != 1
            downloader = FileService(self._paths, self._auth)
            results: list[dict[str, Any]] = []
            candidate_count = 0
            downloaded_count = 0
            skipped_count = 0
            failed_count = 0
            base_root = _resolve_destination_root(files_root=self._paths.files_root, subdir=subdir, dest=dest)

            total = sum(1 for notice in notices for attachment in notice.attachments if str(attachment.get("url") or "").strip())
            current = 0
            for notice in notices:
                for index, attachment in enumerate(notice.attachments):
                    attachment_url = str(attachment.get("url") or "").strip()
                    if not attachment_url:
                        continue
                    current += 1
                    candidate_count += 1
                    resolved_course_id = board_to_course.get(str(notice.board_id or "").strip())
                    course_row = course_meta.get(resolved_course_id or "", {})
                    attachment_title = str(attachment.get("title") or attachment.get("filename") or notice.title or "attachment")
                    _emit_pull_progress(current, total, attachment_title)
                    item = FileItem(
                        id=f"{notice.id or 'notice'}:{index}",
                        title=attachment_title,
                        url=attachment_url,
                        download_url=attachment_url,
                        filename=str(attachment.get("filename") or _attachment_filename_from_url(attachment_url) or "").strip() or None,
                        kind="file",
                        downloadable=True,
                        course_id=resolved_course_id,
                        course_title=course_row.get("course_title") if isinstance(course_row, dict) else None,
                        course_code=course_row.get("course_code") if isinstance(course_row, dict) else None,
                        course_code_base=course_row.get("course_code_base") if isinstance(course_row, dict) else None,
                        source="html:notice-attachment",
                        confidence=0.82,
                        auth_mode=auth_mode,
                    )
                    target_subdir = _pull_subdir_for_item(
                        item,
                        base_subdir=None,
                        include_course_dir=include_course_dirs,
                    )
                    try:
                        result = downloader.download_item_with_context(
                            context=context,
                            config=config,
                            item=item,
                            filename_override=None,
                            subdir=target_subdir,
                            dest=str(base_root),
                            if_exists=if_exists,
                            auth_mode=auth_mode,
                        )
                    except CommandError as exc:
                        failed_count += 1
                        _emit_pull_progress(current, total, attachment_title, status="failed", detail=exc.message)
                        results.append(
                            {
                                "status": "failed",
                                "notice_id": notice.id,
                                "notice_title": notice.title,
                                "board_id": notice.board_id,
                                "course_id": resolved_course_id,
                                "course_title": course_row.get("course_title") if isinstance(course_row, dict) else None,
                                "filename": item.filename,
                                "error": {"code": exc.code, "message": exc.message},
                            }
                        )
                        continue
                    except Exception as exc:
                        failed_count += 1
                        _emit_pull_progress(current, total, attachment_title, status="failed", detail=str(exc))
                        results.append(
                            {
                                "status": "failed",
                                "notice_id": notice.id,
                                "notice_title": notice.title,
                                "board_id": notice.board_id,
                                "course_id": resolved_course_id,
                                "course_title": course_row.get("course_title") if isinstance(course_row, dict) else None,
                                "filename": item.filename,
                                "error": {"code": "DOWNLOAD_FAILED", "message": str(exc)},
                            }
                        )
                        continue

                    if bool(result.get("skipped")):
                        skipped_count += 1
                        _emit_pull_progress(current, total, attachment_title, status="skipped", detail=str(result.get("reason") or "exists"))
                        results.append(
                            {
                                "status": "skipped",
                                "notice_id": notice.id,
                                "notice_title": notice.title,
                                "board_id": notice.board_id,
                                "course_id": resolved_course_id,
                                "course_title": course_row.get("course_title") if isinstance(course_row, dict) else None,
                                "path": result.get("path"),
                                "filename": result.get("filename"),
                                "transport": result.get("transport"),
                            }
                        )
                    else:
                        downloaded_count += 1
                        results.append(
                            {
                                "status": "downloaded",
                                "notice_id": notice.id,
                                "notice_title": notice.title,
                                "board_id": notice.board_id,
                                "course_id": resolved_course_id,
                                "course_title": course_row.get("course_title") if isinstance(course_row, dict) else None,
                                "path": result.get("path"),
                                "filename": result.get("filename"),
                                "transport": result.get("transport"),
                            }
                        )

            payload = {
                "root": str(base_root),
                "course_id": str(course_id).strip() or None if course_id else None,
                "course_query": str(course_query).strip() or None if course_query else None,
                "since_iso": since_iso,
                "if_exists": if_exists,
                "dest": str(base_root) if dest else None,
                "requested_limit": limit,
                "candidate_count": candidate_count,
                "downloaded_count": downloaded_count,
                "skipped_count": skipped_count,
                "failed_count": failed_count,
                "results": results,
            }
            source = "http" if downloaded_count and all(row.get("transport") == "http" for row in results if row.get("status") == "downloaded") else "mixed"
            return CommandResult(data=payload, source=source, capability="degraded" if failed_count else "partial")

        return self._auth.run_authenticated(
            config=config,
            headless=True,
            accept_downloads=True,
            timeout_seconds=10.0,
            callback=callback,
        )

    def _list_html(
        self,
        *,
        context: Any,
        config: KlmsConfig,
        auth_mode: str,
        notice_board_id: str | None,
        course_id: str | None,
        course_query: str | None,
        max_pages: int,
        since_iso: str | None,
        limit: int | None,
        bootstrap: KlmsSessionBootstrap | None = None,
    ) -> list[Notice]:
        bootstrap = bootstrap or build_session_bootstrap(
            self._paths,
            context=context,
            config=config,
            auth_mode=auth_mode,
        )
        board_ids = self._resolve_notice_board_ids(
            context=context,
            config=config,
            explicit_board_id=notice_board_id,
            course_id=course_id,
            course_query=course_query,
            bootstrap=bootstrap,
        )
        if not board_ids:
            raise CommandError(
                code="CONFIG_INVALID",
                message="No notice boards found.",
                hint="Configure notice_board_ids or let v2 discover boards from your course pages.",
                exit_code=40,
            )

        cache_entry = self._load_notice_cache_entry(config=config, board_ids=board_ids, max_pages=max_pages)
        cached_rows = cache_entry.get("value") if isinstance(cache_entry, dict) and not bool(cache_entry.get("stale")) else None
        if isinstance(cached_rows, list):
            cached_items = [Notice(**row) for row in cached_rows if isinstance(row, dict)]
            return _finalize_notice_items(cached_items, since_iso=since_iso, limit=limit)

        all_items = self._refresh_notice_items(
            config=config,
            auth_mode=auth_mode,
            board_ids=board_ids,
            max_pages=max_pages,
            since_iso=since_iso,
            limit=limit,
            bootstrap=bootstrap,
            deadline=None,
        )
        return _finalize_notice_items(all_items, since_iso=since_iso, limit=limit)

    def _refresh_notice_items(
        self,
        *,
        config: KlmsConfig,
        auth_mode: str,
        board_ids: list[str],
        max_pages: int,
        since_iso: str | None,
        limit: int | None,
        bootstrap: KlmsSessionBootstrap,
        deadline: RefreshDeadline | None,
    ) -> list[Notice]:
        first_paths = [f"/mod/courseboard/view.php?id={board_id}" for board_id in board_ids]
        first_responses = fetch_html_batch(
            bootstrap.http,
            first_paths,
            deadline=deadline,
            max_workers=MAX_NOTICE_HTTP_WORKERS,
        )

        all_items: list[Notice] = []
        extra_paths: list[str] = []
        by_board_id: dict[str, list[tuple[str, str]]] = {}
        for board_id, first_path in zip(board_ids, first_paths):
            response = first_responses.get(first_path)
            if response is None:
                continue
            if looks_login_url(response.url) or looks_logged_out_html(response.text):
                raise CommandError(
                    code="AUTH_EXPIRED",
                    message="Saved KLMS auth did not stay authenticated while loading notice boards.",
                    hint="Run `kaist klms auth refresh` and try again.",
                    exit_code=10,
                    retryable=True,
                )
            first_soup = BeautifulSoup(response.text, "html.parser")
            first_page_index, page_sequence = _plan_notice_page_sequence(first_soup, max_pages=max_pages)
            by_board_id.setdefault(board_id, []).append((first_path, response.text))
            for page_index in page_sequence:
                if page_index == first_page_index:
                    continue
                extra_paths.append(f"/mod/courseboard/view.php?id={board_id}&page={page_index}")

        if extra_paths and (deadline is None or not deadline.hard_expired()):
            extra_responses = fetch_html_batch(
                bootstrap.http,
                extra_paths,
                deadline=deadline,
                max_workers=MAX_NOTICE_HTTP_WORKERS,
            )
            for path in extra_paths:
                response = extra_responses.get(path)
                if response is None:
                    continue
                if looks_login_url(response.url) or looks_logged_out_html(response.text):
                    raise CommandError(
                        code="AUTH_EXPIRED",
                        message="Saved KLMS auth did not stay authenticated while loading notice pages.",
                        hint="Run `kaist klms auth refresh` and try again.",
                        exit_code=10,
                        retryable=True,
                    )
                match = re.search(r"[?&]id=(\d+)", path)
                if not match:
                    continue
                by_board_id.setdefault(match.group(1), []).append((path, response.text))

        seen_keys: set[tuple[str, str, str]] = set()
        for board_id in board_ids:
            for page_path, page_html in by_board_id.get(board_id, []):
                if deadline is not None and deadline.hard_expired():
                    raise TimeoutError("Interactive notice refresh budget expired.")
                page_soup = BeautifulSoup(page_html, "html.parser")
                parsed = _parse_notice_items_from_soup(
                    page_soup,
                    board_id=board_id,
                    base_url=config.base_url,
                    fallback_url_path=page_path,
                )
                for item in parsed:
                    key = (board_id, str(item.id or ""), str(item.url or ""))
                    if key in seen_keys:
                        continue
                    seen_keys.add(key)
                    all_items.append(
                        Notice(
                            board_id=item.board_id,
                            id=item.id,
                            title=item.title,
                            url=item.url,
                            posted_raw=item.posted_raw,
                            posted_iso=item.posted_iso,
                            source=item.source,
                            confidence=item.confidence,
                            auth_mode=auth_mode,
                        )
                    )
                if limit is not None and _matching_notice_count(all_items, since_iso=since_iso) >= limit:
                    break
            if limit is not None and _matching_notice_count(all_items, since_iso=since_iso) >= limit:
                break

        final_items = _finalize_notice_items(all_items, since_iso=since_iso, limit=limit)
        final_keys = {
            (str(item.board_id or ""), str(item.id or ""), str(item.url or ""))
            for item in final_items
        }
        if final_items:
            enriched = _enrich_notice_items_from_detail(
                final_items,
                base_url=config.base_url,
                auth_mode=auth_mode,
                bootstrap=bootstrap,
                deadline=deadline,
            )
            enriched_map = {
                (str(item.board_id or ""), str(item.id or ""), str(item.url or "")): item
                for item in enriched
            }
            updated_items: list[Notice] = []
            for item in all_items:
                key = (str(item.board_id or ""), str(item.id or ""), str(item.url or ""))
                updated_items.append(enriched_map.get(key, item) if key in final_keys else item)
            all_items = updated_items

        save_cache_value(self._paths, self._notice_list_cache_key(config, board_ids, max_pages), [item.to_dict() for item in all_items], ttl_seconds=NOTICE_LIST_TTL_SECONDS)
        return all_items

    def _resolve_notice_board_map(
        self,
        *,
        context: Any,
        config: KlmsConfig,
        explicit_board_id: str | None,
        course_id: str | None = None,
        course_query: str | None = None,
        bootstrap: KlmsSessionBootstrap | None = None,
        deadline: RefreshDeadline | None = None,
        allow_stale_cache: bool = False,
    ) -> dict[str, list[str]]:
        if explicit_board_id:
            board_id = str(explicit_board_id).strip()
            return {"": [board_id]} if board_id else {}
        if config.notice_board_ids:
            configured = [str(board_id).strip() for board_id in config.notice_board_ids if str(board_id).strip()]
            return {"": configured} if configured else {}

        if bootstrap is None:
            raise CommandError(
                code="CONFIG_INVALID",
                message="Notice board discovery requires a session bootstrap.",
                exit_code=40,
            )

        course_ids = _extract_course_ids_from_dashboard(
            bootstrap.dashboard_html,
            base_url=config.base_url,
            configured_ids=config.course_ids,
            exclude_patterns=config.exclude_course_title_patterns,
            course_query=course_query,
            course_id=course_id,
        )
        if not course_ids:
            cached_board_ids = self._fallback_notice_board_ids_from_cache(self._paths, config)
            return {"": cached_board_ids} if cached_board_ids else {}

        cache_key = self._notice_board_cache_key(config, course_ids)
        cache_entry = load_cache_entry(self._paths, cache_key)
        cached_rows = cache_entry.get("value") if isinstance(cache_entry, dict) else None
        cached_board_map = cached_rows if isinstance(cached_rows, dict) else {}

        def flatten_board_map(board_map: dict[str, Any]) -> list[str]:
            flattened: list[str] = []
            for selected_course_id in course_ids:
                values = board_map.get(selected_course_id)
                if not isinstance(values, list):
                    continue
                flattened.extend(str(board_id).strip() for board_id in values if str(board_id).strip())
            return list(dict.fromkeys(flattened))

        cached_board_ids = flatten_board_map(cached_board_map)
        if cached_board_ids and (not bool((cache_entry or {}).get("stale")) or allow_stale_cache):
            return {
                selected_course_id: [str(board_id).strip() for board_id in cached_board_map.get(selected_course_id, []) if str(board_id).strip()]
                for selected_course_id in course_ids
                if isinstance(cached_board_map.get(selected_course_id), list)
            }
        if deadline is not None and deadline.hard_expired():
            if allow_stale_cache and cached_board_ids:
                return {
                    selected_course_id: [str(board_id).strip() for board_id in cached_board_map.get(selected_course_id, []) if str(board_id).strip()]
                    for selected_course_id in course_ids
                    if isinstance(cached_board_map.get(selected_course_id), list)
                }
            return {}

        board_map: dict[str, list[str]] = {}
        paths = [f"/course/view.php?id={selected_course_id}&section=0" for selected_course_id in course_ids]
        responses = fetch_html_batch(
            bootstrap.http,
            paths,
            deadline=deadline,
            max_workers=MAX_NOTICE_HTTP_WORKERS,
        )
        for selected_course_id, path in zip(course_ids, paths):
            response = responses.get(path)
            if response is None:
                continue
            if looks_login_url(response.url) or looks_logged_out_html(response.text):
                raise CommandError(
                    code="AUTH_EXPIRED",
                    message="Saved KLMS auth did not stay authenticated while discovering notice boards.",
                    hint="Run `kaist klms auth refresh` and try again.",
                    exit_code=10,
                    retryable=True,
                )
            board_ids = _discover_notice_board_ids_from_course_page(response.text)
            if board_ids:
                board_map[selected_course_id] = board_ids
        if any(board_map.values()):
            save_cache_value(self._paths, cache_key, board_map, ttl_seconds=NOTICE_BOARD_TTL_SECONDS)
            return board_map
        if allow_stale_cache and cached_board_ids:
            return {
                selected_course_id: [str(board_id).strip() for board_id in cached_board_map.get(selected_course_id, []) if str(board_id).strip()]
                for selected_course_id in course_ids
                if isinstance(cached_board_map.get(selected_course_id), list)
            }
        return {}

    def _resolve_notice_board_ids(
        self,
        *,
        context: Any,
        config: KlmsConfig,
        explicit_board_id: str | None,
        course_id: str | None = None,
        course_query: str | None = None,
        bootstrap: KlmsSessionBootstrap | None = None,
        deadline: RefreshDeadline | None = None,
        allow_stale_cache: bool = False,
    ) -> list[str]:
        board_map = self._resolve_notice_board_map(
            context=context,
            config=config,
            explicit_board_id=explicit_board_id,
            course_id=course_id,
            course_query=course_query,
            bootstrap=bootstrap,
            deadline=deadline,
            allow_stale_cache=allow_stale_cache,
        )
        flattened: list[str] = []
        for rows in board_map.values():
            flattened.extend(str(board_id).strip() for board_id in rows if str(board_id).strip())
        return list(dict.fromkeys(flattened))
