from __future__ import annotations

import json
import html as _html
import re
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from bs4 import BeautifulSoup  # type: ignore[import-untyped]

from ..contracts import CommandError, CommandResult
from .auth import AuthService, looks_logged_out_html, looks_login_url
from .cache import load_cache_entry, load_cache_value, save_cache_value
from .config import KlmsConfig, abs_url, load_config
from .courses import (
    _course_code_base,
    _course_matches_query,
    _extract_course_code_from_resource_index,
    _is_noise_course,
    _norm_text,
    _select_dashboard_courses,
)
from .deadline import RefreshDeadline
from .models import FileItem
from .paths import KlmsPaths
from .provider_state import ProviderLoad
from .session import KlmsDownloadFallback, KlmsHttpSession, KlmsSessionBootstrap, build_session_bootstrap, fetch_html_batch
from .validate import looks_klms_error_html

ALLOWED_MODULES = {"resource", "folder", "url", "page", "coursefile"}
DOWNLOADABLE_MODULES = {"resource", "coursefile"}
FILE_LIST_TTL_SECONDS = 15 * 60
FILE_CONTENT_API_STATUS_SUCCESS_TTL_SECONDS = 6 * 60 * 60
FILE_CONTENT_API_STATUS_FAILURE_TTL_SECONDS = 30 * 60
MAX_FILE_HTTP_WORKERS = 4
FILE_CONTENTS_METHOD = "core_course_get_contents"
FILE_AJAX_HEADERS = {
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Content-Type": "application/json",
    "X-Requested-With": "XMLHttpRequest",
}


def _emit_pull_progress(index: int, total: int, title: str, *, status: str | None = None, detail: str | None = None) -> None:
    prefix = f"[{index}/{total}]"
    safe_title = " ".join(str(title or "unnamed item").split()) or "unnamed item"
    if status is None:
        message = f"{prefix} downloading {safe_title} ..."
    else:
        message = f"{prefix} {status} {safe_title}"
        if detail:
            message += f" ({detail})"
    print(message, file=sys.stderr, flush=True)


def _clean_html_text(raw: str) -> str:
    decoded = _html.unescape(raw or "")
    decoded = re.sub(r"<[^>]+>", " ", decoded)
    return _norm_text(decoded)


def _is_video_filename(name: str) -> bool:
    return bool(re.search(r"\.(mp4|mkv|mov|avi|webm|m3u8|ts)$", name, re.IGNORECASE))


def _is_video_url(url: str) -> bool:
    return bool(re.search(r"(m3u8|dash|hls|stream|video)", url, re.IGNORECASE))


def _looks_like_video_item(title: str, url: str) -> bool:
    text = (title or "").lower()
    lowered_url = (url or "").lower()
    keywords = ("video", "lecture video", "panopto", "동영상", "영상", "vod")
    return any(token in text for token in keywords) or any(token in lowered_url for token in ("panopto", "m3u8", "hls", "stream", "/mod/vod/"))


def _material_kind_from_module(module: str | None) -> str:
    return {
        "resource": "file",
        "coursefile": "file",
        "folder": "folder",
        "url": "link",
        "page": "page",
    }.get(module or "", "unknown")


def _filename_from_url(url: str | None) -> str | None:
    if not url:
        return None
    try:
        return Path(unquote(urlparse(url).path or "")).name or None
    except Exception:
        return None


def _sanitize_relpath(rel: str) -> Path:
    text = (rel or "").strip().lstrip("/").replace("\\", "/")
    text = re.sub(r"/+", "/", text)
    parts = [part for part in text.split("/") if part not in ("", ".", "..")]
    return Path(*parts)


def _resolve_destination_root(*, files_root: Path, subdir: str | None, dest: str | None) -> Path:
    if subdir and dest:
        raise CommandError(
            code="CONFIG_INVALID",
            message="--subdir and --dest cannot be used together.",
            hint="Use --dest for an explicit directory, or --subdir for a path under the managed files root.",
            exit_code=40,
        )
    if dest:
        target = Path(str(dest).strip()).expanduser()
        if not str(target).strip():
            raise CommandError(code="CONFIG_INVALID", message="--dest must not be empty.", exit_code=40)
        target.mkdir(parents=True, exist_ok=True)
        return target
    target = files_root / _sanitize_relpath(subdir) if subdir else files_root
    target.mkdir(parents=True, exist_ok=True)
    return target


def _slug_component(text: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(text or "").strip())
    cleaned = re.sub(r"_+", "_", cleaned).strip("._-")
    return cleaned


def _pull_subdir_for_item(item: FileItem, *, base_subdir: str | None, include_course_dir: bool = True) -> str | None:
    course_key = _slug_component(item.course_code or "") or (f"course-{item.course_id}" if item.course_id else "")
    course_label = _slug_component(item.course_title or "")
    if course_key and course_label and course_label.lower() != course_key.lower():
        course_dir = f"{course_key}__{course_label}"
    else:
        course_dir = course_key or course_label

    base_path = _sanitize_relpath(base_subdir) if base_subdir else Path()
    if include_course_dir and course_dir:
        resolved = base_path / course_dir
    else:
        resolved = base_path
    text = str(resolved).strip()
    if text in {"", "."}:
        return None
    return text


def _extract_module_from_url(url: str) -> tuple[str | None, str | None]:
    parsed = urlparse(url)
    match = re.search(r"/mod/([^/]+)/view\.php$", parsed.path or "")
    query = parse_qs(parsed.query, keep_blank_values=True)
    module_id = (query.get("id") or [None])[0]
    module_id_text = str(module_id).strip() if isinstance(module_id, str) else None
    return (match.group(1) if match else None), (module_id_text or None)


def _extract_module_id_from_node(node: Any) -> str | None:
    current = node
    for _ in range(8):
        if current is None:
            break
        attrs = getattr(current, "attrs", None) or {}
        raw_id = str(attrs.get("id") or "").strip()
        match = re.search(r"(?:^|[^0-9])module-(\d+)$", raw_id)
        if match:
            return match.group(1)
        current = getattr(current, "parent", None)
    return None


def _looks_like_direct_file_url(url: str) -> bool:
    lowered = url.lower()
    return "pluginfile.php" in lowered or "forcedownload=1" in lowered or bool(
        re.search(r"\.(pdf|zip|7z|tar|gz|hwp|hwpx|doc|docx|ppt|pptx|xls|xlsx|txt|csv|py|ipynb)$", lowered)
    )


def _looks_like_url_candidate(value: str) -> bool:
    text = str(value or "").strip()
    return text.startswith(("http://", "https://", "/")) or any(token in text for token in (".php", "pluginfile.php", "/mod/"))


def _looks_like_material_target_url(url: str) -> bool:
    if _looks_like_direct_file_url(url):
        return True
    module, module_id = _extract_module_from_url(url)
    return bool(module_id and module in ALLOWED_MODULES)


def _extract_material_title_from_page(html: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    for selector in ("#page-header h1", ".page-header-headings h1", ".subject h3", "h1", "title"):
        node = soup.select_one(selector)
        if not node:
            continue
        title = _norm_text(node.get_text(" ", strip=True))
        if title:
            return title
    return None


def _course_map_from_dashboard(html: str, *, base_url: str, configured_ids: tuple[str, ...]) -> dict[str, dict[str, str | None]]:
    courses = {
        str(course.id): {
            "course_id": str(course.id),
            "course_title": course.title,
            "course_code": course.course_code,
            "term_label": course.term_label,
        }
        for course in _select_dashboard_courses(
            html,
            base_url=base_url,
            exclude_patterns=(),
            course_query=None,
            include_past=True,
            allow_termless_fallback=True,
        )
    }
    for configured_id in configured_ids:
        course_id = str(configured_id).strip()
        if not course_id:
            continue
        courses.setdefault(course_id, {"course_id": course_id, "course_title": None, "course_code": None, "term_label": None})
    return courses


def _build_file_item(
    *,
    url: str,
    title: str,
    course_id: str | None,
    course_title: str | None,
    course_code: str | None,
    auth_mode: str | None,
    source: str,
    confidence: float,
) -> FileItem | None:
    module, module_id = _extract_module_from_url(url)
    is_direct_file = _looks_like_direct_file_url(url)
    if module and module not in ALLOWED_MODULES and not is_direct_file:
        return None
    kind = "file" if is_direct_file else _material_kind_from_module(module)
    downloadable = is_direct_file or module in DOWNLOADABLE_MODULES
    filename = _filename_from_url(url) if is_direct_file else None
    normalized_title = _norm_text(title) or filename or (f"material-{module_id}" if module_id else None)
    if not normalized_title:
        return None
    if kind == "unknown" and not is_direct_file:
        return None
    if _looks_like_video_item(normalized_title, url) or (filename and _is_video_filename(filename)) or _is_video_url(url):
        return None
    return FileItem(
        id=module_id,
        title=normalized_title,
        url=url,
        download_url=url if downloadable else None,
        filename=filename,
        kind=kind,
        downloadable=bool(downloadable),
        course_id=course_id,
        course_title=course_title,
        course_code=course_code,
        course_code_base=_course_code_base(course_code),
        source=source,
        confidence=confidence,
        auth_mode=auth_mode,
    )


def _merge_file_items(items: list[FileItem]) -> list[FileItem]:
    merged: dict[str, FileItem] = {}
    for item in items:
        key = str(item.url or "")
        if not key:
            continue
        existing = merged.get(key)
        if existing is None:
            merged[key] = item
            continue
        winner = existing if existing.confidence >= item.confidence else item
        loser = item if winner is existing else existing
        merged[key] = replace(
            winner,
            id=winner.id or loser.id,
            title=winner.title if len(winner.title) >= len(loser.title) else loser.title,
            download_url=winner.download_url or loser.download_url,
            filename=winner.filename or loser.filename,
            course_id=winner.course_id or loser.course_id,
            course_title=winner.course_title or loser.course_title,
            course_code=winner.course_code or loser.course_code,
            course_code_base=winner.course_code_base or loser.course_code_base,
            source=winner.source if winner.source == loser.source else "mixed:file-surface",
            confidence=max(winner.confidence, loser.confidence),
            auth_mode=winner.auth_mode or loser.auth_mode,
        )
    return list(merged.values())


def _extract_file_items_from_html(
    html: str,
    *,
    base_url: str,
    course_id: str | None,
    course_title: str | None,
    course_code: str | None,
    auth_mode: str | None,
    source: str,
) -> list[FileItem]:
    soup = BeautifulSoup(html, "html.parser")
    out: list[FileItem] = []

    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href") or "").strip()
        if not href:
            continue
        if href.lower().startswith("javascript:") or href.startswith("#"):
            continue
        if "/mod/resource/index.php" in href:
            continue
        if "/mod/courseboard/" in href or "/mod/assign/" in href or "/mod/vod/" in href or "/mod/zoom/" in href:
            continue
        url = abs_url(base_url, href)
        title = _norm_text(anchor.get_text(" ", strip=True))
        if not title:
            title = _norm_text(str(anchor.get("title") or anchor.get("aria-label") or ""))
        if not title:
            title = _filename_from_url(url) or ""
        if title.lower() in {"course contents", "files"}:
            continue
        item = _build_file_item(
            url=url,
            title=title,
            course_id=course_id,
            course_title=course_title,
            course_code=course_code,
            auth_mode=auth_mode,
            source=source,
            confidence=0.7 if source == "html:resource-index" else 0.66,
        )
        if item is not None:
            out.append(item)

    for node in soup.select("[onclick*='downloadFile(']"):
        onclick = _html.unescape(str(node.get("onclick") or "")).strip()
        match = re.search(
            r"downloadFile\(\s*['\"]([^'\"]+)['\"]\s*,\s*['\"]([^'\"]+)['\"]\s*\)",
            onclick,
        )
        if not match:
            continue
        url = abs_url(base_url, _html.unescape(match.group(1)))
        title = _norm_text(node.get_text(" ", strip=True))
        title = re.sub(r"\bFile\s*$", "", title, flags=re.IGNORECASE).strip() or _clean_html_text(match.group(2))
        item = _build_file_item(
            url=url,
            title=title,
            course_id=course_id,
            course_title=course_title,
            course_code=course_code,
            auth_mode=auth_mode,
            source="html:downloadFile-js",
            confidence=0.74,
        )
        if item is not None:
            out.append(
                replace(
                    item,
                    id=item.id or _extract_module_id_from_node(node),
                    filename=item.filename or _norm_text(_html.unescape(match.group(2))) or _filename_from_url(url),
                    download_url=url,
                    downloadable=True,
                )
            )

    for match in re.finditer(
        r"downloadFile\(\s*(?:&#0*39;|['\"])([^'\"<]+?)(?:&#0*39;|['\"])\s*,\s*(?:&#0*39;|['\"])(.+?)(?:&#0*39;|['\"])\s*\)",
        html,
        flags=re.DOTALL,
    ):
        url = abs_url(base_url, _html.unescape(match.group(1)))
        title = _clean_html_text(match.group(2))
        item = _build_file_item(
            url=url,
            title=title,
            course_id=course_id,
            course_title=course_title,
            course_code=course_code,
            auth_mode=auth_mode,
            source="html:downloadFile-js",
            confidence=0.74,
        )
        if item is not None:
            out.append(item)

    return _merge_file_items(out)


def _synthesize_file_item_from_url(
    url: str,
    *,
    course_id: str | None,
    course_title: str | None,
    course_code: str | None,
    auth_mode: str | None,
) -> FileItem:
    item = _build_file_item(
        url=url,
        title=_filename_from_url(url) or url.rstrip("/").split("/")[-1] or "file",
        course_id=course_id,
        course_title=course_title,
        course_code=course_code,
        auth_mode=auth_mode,
        source="url:synthetic",
        confidence=0.45,
    )
    if item is None:
        module, module_id = _extract_module_from_url(url)
        return FileItem(
            id=module_id,
            title=f"material-{module_id}" if module_id else "file",
            url=url,
            download_url=url if _looks_like_direct_file_url(url) else None,
            filename=_filename_from_url(url),
            kind=_material_kind_from_module(module),
            downloadable=_looks_like_direct_file_url(url) or module in DOWNLOADABLE_MODULES,
            course_id=course_id,
            course_title=course_title,
            course_code=course_code,
            course_code_base=_course_code_base(course_code),
            source="url:synthetic",
            confidence=0.35,
            auth_mode=auth_mode,
        )
    return item


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


def _provider_warning(code: str, message: str, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"code": code, "message": message}
    payload.update(extra)
    return payload


def _file_provider_source(items: list[FileItem]) -> str:
    has_api = any(str(item.source or "").startswith("api:") for item in items)
    has_html = any(str(item.source or "").startswith("html:") for item in items)
    if has_api and has_html:
        return "mixed"
    if has_api:
        return "api"
    return "html"


def _unwrap_moodle_ajax_payload(text: str) -> dict[str, Any]:
    try:
        payload = json.loads(text)
    except Exception as exc:  # noqa: BLE001
        return {"status": "invalid", "message": f"Failed to parse Moodle AJAX JSON: {exc}"}
    if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
        return {"status": "invalid", "message": "Unexpected Moodle AJAX response shape."}
    first = payload[0]
    if bool(first.get("error")):
        exception = first.get("exception") if isinstance(first.get("exception"), dict) else {}
        return {
            "status": "error",
            "error_code": str(exception.get("errorcode") or first.get("errorcode") or "").strip() or None,
            "message": str(exception.get("message") or first.get("message") or "Moodle AJAX returned an error payload.").strip(),
            "exception": exception,
        }
    return {"status": "ok", "data": first.get("data")}


def _extract_file_items_from_course_contents(
    data: Any,
    *,
    base_url: str,
    course_id: str | None,
    course_title: str | None,
    course_code: str | None,
    auth_mode: str | None,
) -> list[FileItem]:
    if not isinstance(data, list):
        return []

    out: list[FileItem] = []
    for section in data:
        if not isinstance(section, dict):
            continue
        modules = section.get("modules")
        if not isinstance(modules, list):
            continue
        for module in modules:
            if not isinstance(module, dict):
                continue
            modname = str(module.get("modname") or "").strip().lower()
            if modname not in ALLOWED_MODULES:
                continue
            if module.get("uservisible") is False or module.get("visible") is False:
                continue
            module_id = str(module.get("id") or module.get("cmid") or "").strip() or None
            module_url_raw = str(module.get("url") or "").strip()
            module_url = abs_url(base_url, module_url_raw) if module_url_raw else None
            title = _norm_text(str(module.get("name") or "")) or _filename_from_url(module_url) or (f"material-{module_id}" if module_id else None)
            if not title:
                continue

            content_file_url: str | None = None
            content_filename: str | None = None
            contents = module.get("contents")
            if isinstance(contents, list):
                for content in contents:
                    if not isinstance(content, dict):
                        continue
                    file_url_raw = str(content.get("fileurl") or content.get("downloadurl") or content.get("url") or "").strip()
                    if not file_url_raw:
                        continue
                    candidate_url = abs_url(base_url, file_url_raw)
                    if _looks_like_video_item(title, candidate_url):
                        continue
                    content_file_url = candidate_url
                    content_filename = str(content.get("filename") or "").strip() or _filename_from_url(candidate_url)
                    break

            item_url = module_url or content_file_url
            if not item_url:
                continue
            download_url = None
            downloadable = False
            filename = None
            if modname in DOWNLOADABLE_MODULES:
                download_url = content_file_url or module_url
                downloadable = bool(download_url)
                filename = content_filename or _filename_from_url(download_url)
            kind = _material_kind_from_module(modname)
            if _looks_like_video_item(title, download_url or item_url) or (filename and _is_video_filename(filename)):
                continue
            out.append(
                FileItem(
                    id=module_id,
                    title=title,
                    url=item_url,
                    download_url=download_url,
                    filename=filename,
                    kind=kind,
                    downloadable=downloadable,
                    course_id=course_id,
                    course_title=course_title,
                    course_code=course_code,
                    course_code_base=_course_code_base(course_code),
                    source=f"api:{FILE_CONTENTS_METHOD}",
                    confidence=0.88 if downloadable else 0.82,
                    auth_mode=auth_mode,
                )
            )
    return _merge_file_items(out)


class FileService:
    def __init__(self, paths: KlmsPaths, auth: AuthService) -> None:
        self._paths = paths
        self._auth = auth

    @staticmethod
    def _file_list_cache_key(config: KlmsConfig, course_ids: list[str]) -> str:
        return "::".join(
            [
                "file-list",
                config.base_url.rstrip("/"),
                ",".join(course_ids),
            ]
        )

    @staticmethod
    def _content_api_status_cache_key(config: KlmsConfig) -> str:
        return "::".join(["file-content-api-status", config.base_url.rstrip("/")])

    def list_with_context(
        self,
        *,
        context: Any,
        config: KlmsConfig,
        auth_mode: str,
        course_id: str | None = None,
        course_query: str | None = None,
        limit: int | None = None,
        bootstrap: KlmsSessionBootstrap | None = None,
    ) -> CommandResult:
        bootstrap = bootstrap or build_session_bootstrap(
            self._paths,
            context=context,
            config=config,
            auth_mode=auth_mode,
        )
        items = self._list_html(
            context=context,
            config=config,
            auth_mode=auth_mode,
            course_id=course_id,
            course_query=course_query,
            limit=limit,
            bootstrap=bootstrap,
        )
        return CommandResult(data=[item.to_dict() for item in items], source=_file_provider_source(items), capability="partial")

    def load_for_dashboard(
        self,
        *,
        context: Any,
        config: KlmsConfig,
        auth_mode: str,
        course_id: str | None = None,
        course_query: str | None = None,
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
        course_map = self._course_map_for_request(bootstrap=bootstrap, config=config, course_id=course_id, course_query=course_query)
        cache_key = self._file_list_cache_key(config, list(course_map.keys()))
        cache_entry = load_cache_entry(self._paths, cache_key)
        cached_rows = cache_entry.get("value") if isinstance(cache_entry, dict) else None
        cached_items = [FileItem(**row) for row in cached_rows if isinstance(row, dict)] if isinstance(cached_rows, list) else []
        cached_items.sort(key=lambda item: (item.course_title or "", item.kind, item.title.lower()))
        cached_limited = cached_items[: max(0, limit)] if limit is not None else cached_items
        cached_source = _file_provider_source(cached_limited)
        cache_fresh_enough = _cache_is_fresh_enough(cache_entry)
        if prefer_cache and cache_entry is not None and (not bool(cache_entry.get("stale")) or cache_fresh_enough):
            return ProviderLoad(
                items=[item.to_dict() for item in cached_limited],
                source=cached_source,
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
            if cache_entry is not None and cached_limited:
                warnings: list[dict[str, Any]] = []
                if not _cache_is_fresh_enough(cache_entry):
                    warnings.append(_provider_warning("LIVE_REFRESH_TIMEOUT", "Interactive refresh budget expired before file refresh completed."))
                if bool(cache_entry.get("stale")):
                    warnings.insert(0, _provider_warning("STALE_CACHE", "Returning stale file cache because live refresh could not finish in time."))
                return ProviderLoad(
                    items=[item.to_dict() for item in cached_limited],
                    source=cached_source,
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
                    _provider_warning("LIVE_REFRESH_TIMEOUT", "Interactive refresh budget expired before file refresh started."),
                ),
            )

        try:
            live_items = self._refresh_file_items(
                config=config,
                auth_mode=auth_mode,
                course_map=course_map,
                limit=limit,
                bootstrap=bootstrap,
                deadline=deadline,
            )
        except TimeoutError:
            if cache_entry is not None and cached_limited:
                warnings: list[dict[str, Any]] = []
                if not _cache_is_fresh_enough(cache_entry):
                    warnings.append(_provider_warning("LIVE_REFRESH_TIMEOUT", "File refresh exceeded the interactive deadline."))
                if bool(cache_entry.get("stale")):
                    warnings.insert(0, _provider_warning("STALE_CACHE", "Returning stale file cache because live refresh timed out."))
                return ProviderLoad(
                    items=[item.to_dict() for item in cached_limited],
                    source=cached_source,
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
                    _provider_warning("LIVE_REFRESH_TIMEOUT", "File refresh exceeded the interactive deadline."),
                ),
            )
        except CommandError:
            raise
        except Exception as exc:
            if cache_entry is not None and cached_limited:
                warnings: list[dict[str, Any]] = []
                if not _cache_is_fresh_enough(cache_entry):
                    warnings.append(_provider_warning("LIVE_REFRESH_FAILED", "File refresh failed; returning cached file data.", error=str(exc)))
                if bool(cache_entry.get("stale")):
                    warnings.insert(0, _provider_warning("STALE_CACHE", "Returning stale file cache because live refresh failed."))
                return ProviderLoad(
                    items=[item.to_dict() for item in cached_limited],
                    source=cached_source,
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
                    _provider_warning("LIVE_REFRESH_FAILED", "File refresh failed.", error=str(exc)),
                ),
            )

        fresh_entry = load_cache_entry(self._paths, cache_key)
        return ProviderLoad(
            items=[item.to_dict() for item in live_items],
            source=_file_provider_source(live_items),
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
        course_id: str | None = None,
        bootstrap: KlmsSessionBootstrap | None = None,
    ) -> ProviderLoad:
        return self.load_for_dashboard(
            context=context,
            config=config,
            auth_mode=auth_mode,
            course_id=course_id,
            limit=None,
            bootstrap=bootstrap,
            deadline=None,
            prefer_cache=False,
        )

    def list(self, *, course_id: str | None = None, course_query: str | None = None, limit: int | None = None) -> CommandResult:
        config = load_config(self._paths)

        def callback(context: Any, auth_mode: str) -> CommandResult:
            return self.list_with_context(
                context=context,
                config=config,
                auth_mode=auth_mode,
                course_id=course_id,
                course_query=course_query,
                limit=limit,
            )

        return self._auth.run_authenticated(
            config=config,
            headless=True,
            accept_downloads=False,
            timeout_seconds=10.0,
            callback=callback,
        )

    def get(self, file_id_or_url: str) -> CommandResult:
        config = load_config(self._paths)
        target = str(file_id_or_url).strip()
        if not target:
            raise CommandError(code="CONFIG_INVALID", message="File ID or URL is required.", exit_code=40)

        def callback(context: Any, auth_mode: str) -> CommandResult:
            resolved = self._resolve_target_item(context=context, config=config, auth_mode=auth_mode, target=target)
            return CommandResult(data=resolved.to_dict(), source="api" if str(resolved.source or "").startswith("api:") else "html", capability="partial")

        return self._auth.run_authenticated(
            config=config,
            headless=True,
            accept_downloads=False,
            timeout_seconds=10.0,
            callback=callback,
        )

    def download(
        self,
        file_id_or_url: str,
        *,
        filename: str | None = None,
        subdir: str | None = None,
        dest: str | None = None,
        if_exists: str = "skip",
    ) -> CommandResult:
        if subdir and dest:
            raise CommandError(
                code="CONFIG_INVALID",
                message="--subdir and --dest cannot be used together.",
                hint="Use --dest for an explicit directory, or --subdir for a path under the managed files root.",
                exit_code=40,
            )
        config = load_config(self._paths)
        target = str(file_id_or_url).strip()
        if not target:
            raise CommandError(code="CONFIG_INVALID", message="File ID or URL is required.", exit_code=40)
        if if_exists not in {"skip", "overwrite"}:
            raise CommandError(code="CONFIG_INVALID", message="if_exists must be 'skip' or 'overwrite'.", exit_code=40)

        def callback(context: Any, auth_mode: str) -> CommandResult:
            item = self._resolve_target_item(context=context, config=config, auth_mode=auth_mode, target=target)
            if not item.downloadable or not item.download_url:
                raise CommandError(
                    code="NOT_DOWNLOADABLE",
                    message=f"Item is not directly downloadable: {item.title}",
                    hint="Use `kaist klms files get` to inspect the material metadata first.",
                    exit_code=40,
                )
            result = self._download_resolved_item(
                context=context,
                config=config,
                item=item,
                filename_override=filename,
                subdir=subdir,
                dest=dest,
                if_exists=if_exists,
                auth_mode=auth_mode,
            )
            return CommandResult(data=result, source=str(result.get("transport") or "browser"), capability="partial")

        return self._auth.run_authenticated(
            config=config,
            headless=True,
            accept_downloads=True,
            timeout_seconds=10.0,
            callback=callback,
        )

    def pull(
        self,
        *,
        course_id: str | None = None,
        course_query: str | None = None,
        limit: int | None = None,
        subdir: str | None = None,
        dest: str | None = None,
        if_exists: str = "skip",
    ) -> CommandResult:
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
            items = self._list_html(
                context=context,
                config=config,
                auth_mode=auth_mode,
                course_id=course_id,
                course_query=course_query,
                limit=limit,
                bootstrap=bootstrap,
            )
            candidates = [item for item in items if item.downloadable and str(item.download_url or item.url or "").strip()]
            if limit is not None:
                candidates = candidates[: max(0, limit)]
            candidate_course_ids = {str(item.course_id).strip() for item in candidates if str(item.course_id or "").strip()}
            include_course_dirs = len(candidate_course_ids) != 1

            results: list[dict[str, Any]] = []
            downloaded_count = 0
            skipped_count = 0
            failed_count = 0
            base_root = _resolve_destination_root(files_root=self._paths.files_root, subdir=subdir, dest=dest)

            total = len(candidates)
            for index, item in enumerate(candidates, start=1):
                _emit_pull_progress(index, total, item.title)
                target_subdir = _pull_subdir_for_item(
                    item,
                    base_subdir=None,
                    include_course_dir=include_course_dirs,
                )
                try:
                    result = self._download_resolved_item(
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
                    _emit_pull_progress(index, total, item.title, status="failed", detail=exc.message)
                    results.append(
                        {
                            "status": "failed",
                            "id": item.id,
                            "title": item.title,
                            "course_id": item.course_id,
                            "course_title": item.course_title,
                            "error": {"code": exc.code, "message": exc.message},
                        }
                    )
                    continue
                except Exception as exc:  # noqa: BLE001
                    failed_count += 1
                    _emit_pull_progress(index, total, item.title, status="failed", detail=str(exc))
                    results.append(
                        {
                            "status": "failed",
                            "id": item.id,
                            "title": item.title,
                            "course_id": item.course_id,
                            "course_title": item.course_title,
                            "error": {"code": "DOWNLOAD_FAILED", "message": str(exc)},
                        }
                    )
                    continue

                if bool(result.get("skipped")):
                    skipped_count += 1
                    _emit_pull_progress(index, total, item.title, status="skipped", detail=str(result.get("reason") or "exists"))
                    results.append(
                        {
                            "status": "skipped",
                            "reason": result.get("reason"),
                            "id": item.id,
                            "title": item.title,
                            "course_id": item.course_id,
                            "course_title": item.course_title,
                            "path": result.get("path"),
                            "filename": result.get("filename"),
                        }
                    )
                else:
                    downloaded_count += 1
                    results.append(
                        {
                            "status": "downloaded",
                            "id": item.id,
                            "title": item.title,
                            "course_id": item.course_id,
                            "course_title": item.course_title,
                            "path": result.get("path"),
                            "filename": result.get("filename"),
                            "transport": result.get("transport"),
                        }
                    )

            payload = {
                "root": str(base_root),
                "course_id": str(course_id).strip() or None if course_id else None,
                "course_query": str(course_query).strip() or None if course_query else None,
                "if_exists": if_exists,
                "dest": str(base_root) if dest else None,
                "requested_limit": limit,
                "candidate_count": len(candidates),
                "downloaded_count": downloaded_count,
                "skipped_count": skipped_count,
                "failed_count": failed_count,
                "results": results,
            }
            return CommandResult(
                data=payload,
                source="http" if downloaded_count and all(result.get("transport") == "http" for result in results if result.get("status") == "downloaded") else "mixed",
                capability="degraded" if failed_count else "partial",
            )

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
        course_id: str | None,
        course_query: str | None,
        limit: int | None,
        bootstrap: KlmsSessionBootstrap | None = None,
    ) -> list[FileItem]:
        bootstrap = bootstrap or build_session_bootstrap(
            self._paths,
            context=context,
            config=config,
            auth_mode=auth_mode,
        )
        course_map = self._course_map_for_request(bootstrap=bootstrap, config=config, course_id=course_id, course_query=course_query)
        if not course_map:
            return []

        cache_key = self._file_list_cache_key(config, list(course_map.keys()))
        cached_rows = load_cache_value(self._paths, cache_key)
        if isinstance(cached_rows, list):
            cached_items = [FileItem(**row) for row in cached_rows if isinstance(row, dict)]
            cached_items.sort(key=lambda item: (item.course_title or "", item.kind, item.title.lower()))
            if limit is not None:
                cached_items = cached_items[: max(0, limit)]
            return cached_items

        deduped = self._refresh_file_items(
            config=config,
            auth_mode=auth_mode,
            course_map=course_map,
            limit=limit,
            bootstrap=bootstrap,
            deadline=None,
        )
        if limit is not None:
            deduped = deduped[: max(0, limit)]
        return deduped

    def _course_map_for_request(
        self,
        *,
        bootstrap: KlmsSessionBootstrap,
        config: KlmsConfig,
        course_id: str | None,
        course_query: str | None = None,
    ) -> dict[str, dict[str, str | None]]:
        course_map = _course_map_from_dashboard(bootstrap.dashboard_html, base_url=config.base_url, configured_ids=config.course_ids)
        if course_id:
            target = str(course_id).strip()
            course_meta = course_map.get(target) or {"course_id": target, "course_title": None, "course_code": None, "term_label": None}
            return {target: course_meta}
        discovered = _select_dashboard_courses(
            bootstrap.dashboard_html,
            base_url=config.base_url,
            exclude_patterns=config.exclude_course_title_patterns,
            course_query=course_query,
            include_past=False,
            allow_termless_fallback=True,
        )
        selected_ids = {str(course.id).strip() for course in discovered if str(course.id).strip()}
        return {
            key: value
            for key, value in course_map.items()
            if key in selected_ids
            and not _is_noise_course(str(value.get("course_title") or ""), config.exclude_course_title_patterns)
            and _course_matches_query(
                type(
                    "_CourseQueryCarrier",
                    (),
                    {
                        "id": key,
                        "title": str(value.get("course_title") or ""),
                        "course_code": value.get("course_code"),
                        "course_code_base": _course_code_base(str(value.get("course_code") or "").strip() or None),
                    },
                ),
                course_query,
            )
        }

    def _refresh_file_items(
        self,
        *,
        config: KlmsConfig,
        auth_mode: str,
        course_map: dict[str, dict[str, str | None]],
        limit: int | None,
        bootstrap: KlmsSessionBootstrap,
        deadline: RefreshDeadline | None,
    ) -> list[FileItem]:
        items: list[FileItem] = []
        api_items, api_covered_course_ids = self._refresh_file_items_api(
            config=config,
            auth_mode=auth_mode,
            course_map=course_map,
            limit=limit,
            bootstrap=bootstrap,
            deadline=deadline,
        )
        items.extend(api_items)
        if limit is not None and sum(1 for item in _merge_file_items(items) if item.downloadable) >= limit:
            deduped = _merge_file_items(items)
            deduped.sort(key=lambda item: (item.course_title or "", item.kind, item.title.lower()))
            save_cache_value(self._paths, self._file_list_cache_key(config, list(course_map.keys())), [item.to_dict() for item in deduped], ttl_seconds=FILE_LIST_TTL_SECONDS)
            return deduped

        course_meta_list = [
            course_meta
            for course_meta in course_map.values()
            if str(course_meta.get("course_id") or "").strip() not in api_covered_course_ids
        ]
        for start in range(0, len(course_meta_list), MAX_FILE_HTTP_WORKERS):
            if deadline is not None and deadline.hard_expired():
                raise TimeoutError("Interactive file refresh budget expired.")

            batch = course_meta_list[start : start + MAX_FILE_HTTP_WORKERS]
            index_paths = []
            path_to_course: dict[str, dict[str, str | None]] = {}
            for course_meta in batch:
                current_course_id = str(course_meta.get("course_id") or "").strip() or None
                if not current_course_id:
                    continue
                path = f"/mod/resource/index.php?id={current_course_id}"
                index_paths.append(path)
                path_to_course[path] = course_meta

            index_responses = fetch_html_batch(
                bootstrap.http,
                index_paths,
                deadline=deadline,
                max_workers=MAX_FILE_HTTP_WORKERS,
            )
            for path in index_paths:
                response = index_responses.get(path)
                if response is None:
                    continue
                if looks_login_url(response.url) or looks_logged_out_html(response.text):
                    raise CommandError(
                        code="AUTH_EXPIRED",
                        message="Saved KLMS auth did not stay authenticated while loading file indexes.",
                        hint="Run `kaist klms auth refresh` and try again.",
                        exit_code=10,
                        retryable=True,
                    )
                course_meta = path_to_course[path]
                current_course_id = str(course_meta.get("course_id") or "").strip() or None
                current_course_title = str(course_meta.get("course_title") or "").strip() or None
                current_course_code = str(course_meta.get("course_code") or "").strip() or None
                current_course_code = current_course_code or _extract_course_code_from_resource_index(response.text)
                items.extend(
                    _extract_file_items_from_html(
                        response.text,
                        base_url=config.base_url,
                        course_id=current_course_id,
                        course_title=current_course_title,
                        course_code=current_course_code,
                        auth_mode=auth_mode,
                        source="html:resource-index",
                    )
                )
                if limit is not None and sum(1 for item in _merge_file_items(items) if item.downloadable) >= limit:
                    break
            if limit is not None and sum(1 for item in _merge_file_items(items) if item.downloadable) >= limit:
                break

            course_paths = []
            path_to_course = {}
            for course_meta in batch:
                current_course_id = str(course_meta.get("course_id") or "").strip() or None
                if not current_course_id:
                    continue
                path = f"/course/view.php?id={current_course_id}&section=0"
                course_paths.append(path)
                path_to_course[path] = course_meta

            course_responses = fetch_html_batch(
                bootstrap.http,
                course_paths,
                deadline=deadline,
                max_workers=MAX_FILE_HTTP_WORKERS,
            )
            for path in course_paths:
                response = course_responses.get(path)
                if response is None:
                    continue
                if looks_login_url(response.url) or looks_logged_out_html(response.text):
                    raise CommandError(
                        code="AUTH_EXPIRED",
                        message="Saved KLMS auth did not stay authenticated while loading course materials.",
                        hint="Run `kaist klms auth refresh` and try again.",
                        exit_code=10,
                        retryable=True,
                    )
                course_meta = path_to_course[path]
                current_course_id = str(course_meta.get("course_id") or "").strip() or None
                current_course_title = str(course_meta.get("course_title") or "").strip() or None
                current_course_code = str(course_meta.get("course_code") or "").strip() or None
                items.extend(
                    _extract_file_items_from_html(
                        response.text,
                        base_url=config.base_url,
                        course_id=current_course_id,
                        course_title=current_course_title,
                        course_code=current_course_code,
                        auth_mode=auth_mode,
                        source="html:course-view",
                    )
                )
                if limit is not None and sum(1 for item in _merge_file_items(items) if item.downloadable) >= limit:
                    break
            if limit is not None and sum(1 for item in _merge_file_items(items) if item.downloadable) >= limit:
                break

        deduped = _merge_file_items(items)
        deduped.sort(key=lambda item: (item.course_title or "", item.kind, item.title.lower()))
        save_cache_value(self._paths, self._file_list_cache_key(config, list(course_map.keys())), [item.to_dict() for item in deduped], ttl_seconds=FILE_LIST_TTL_SECONDS)
        return deduped

    def _refresh_file_items_api(
        self,
        *,
        config: KlmsConfig,
        auth_mode: str,
        course_map: dict[str, dict[str, str | None]],
        limit: int | None,
        bootstrap: KlmsSessionBootstrap,
        deadline: RefreshDeadline | None,
    ) -> tuple[list[FileItem], set[str]]:
        course_ids = [str(course_id).strip() for course_id in course_map.keys() if str(course_id).strip()]
        if not course_ids:
            return ([], set())
        status = self._course_contents_api_status(
            config=config,
            bootstrap=bootstrap,
            deadline=deadline,
            course_ids=course_ids,
        )
        if not bool(status.get("available")):
            return ([], set())

        items: list[FileItem] = []
        covered_course_ids: set[str] = set()
        for course_id in course_ids:
            if deadline is not None and deadline.hard_expired():
                raise TimeoutError("Interactive file refresh budget expired.")
            course_meta = course_map.get(course_id) or {"course_id": course_id, "course_title": None, "course_code": None}
            result = self._call_course_contents_api(
                config=config,
                bootstrap=bootstrap,
                course_id=course_id,
                deadline=deadline,
            )
            if result["status"] == "ok":
                covered_course_ids.add(course_id)
                items.extend(
                    _extract_file_items_from_course_contents(
                        result.get("data"),
                        base_url=config.base_url,
                        course_id=course_id,
                        course_title=str(course_meta.get("course_title") or "").strip() or None,
                        course_code=str(course_meta.get("course_code") or "").strip() or None,
                        auth_mode=auth_mode,
                    )
                )
                if limit is not None and sum(1 for item in _merge_file_items(items) if item.downloadable) >= limit:
                    break
                continue
            if str(result.get("error_code") or "") == "servicenotavailable":
                save_cache_value(
                    self._paths,
                    self._content_api_status_cache_key(config),
                    {
                        "available": False,
                        "error_code": "servicenotavailable",
                        "message": str(result.get("message") or ""),
                    },
                    ttl_seconds=FILE_CONTENT_API_STATUS_FAILURE_TTL_SECONDS,
                )
                break
        return (_merge_file_items(items), covered_course_ids)

    def _course_contents_api_status(
        self,
        *,
        config: KlmsConfig,
        bootstrap: KlmsSessionBootstrap,
        deadline: RefreshDeadline | None,
        course_ids: list[str],
    ) -> dict[str, Any]:
        cache_key = self._content_api_status_cache_key(config)
        cached = load_cache_value(self._paths, cache_key)
        if isinstance(cached, dict) and "available" in cached:
            return cached
        if not bootstrap.dashboard_sesskey or not course_ids:
            return {"available": False, "error_code": "missing_sesskey", "message": "Dashboard sesskey was unavailable."}
        result = self._call_course_contents_api(
            config=config,
            bootstrap=bootstrap,
            course_id=course_ids[0],
            deadline=deadline,
        )
        if result["status"] == "ok":
            payload = {"available": True, "sample_course_id": course_ids[0]}
            save_cache_value(self._paths, cache_key, payload, ttl_seconds=FILE_CONTENT_API_STATUS_SUCCESS_TTL_SECONDS)
            return payload
        payload = {
            "available": False,
            "error_code": result.get("error_code"),
            "message": result.get("message"),
            "sample_course_id": course_ids[0],
        }
        save_cache_value(self._paths, cache_key, payload, ttl_seconds=FILE_CONTENT_API_STATUS_FAILURE_TTL_SECONDS)
        return payload

    def _call_course_contents_api(
        self,
        *,
        config: KlmsConfig,
        bootstrap: KlmsSessionBootstrap,
        course_id: str,
        deadline: RefreshDeadline | None,
    ) -> dict[str, Any]:
        sesskey = str(bootstrap.dashboard_sesskey or "").strip()
        if not sesskey:
            return {"status": "invalid", "message": "Dashboard sesskey was unavailable."}
        ajax_path = f"/lib/ajax/service.php?sesskey={sesskey}&info={FILE_CONTENTS_METHOD}"
        payload = [{"index": 0, "methodname": FILE_CONTENTS_METHOD, "args": {"courseid": int(course_id)}}]
        timeout_seconds = deadline.request_timeout(6.0, use_soft=False) if deadline is not None else 6.0
        response = bootstrap.http.post_text(
            ajax_path,
            body=json.dumps(payload),
            headers=FILE_AJAX_HEADERS,
            timeout_seconds=timeout_seconds,
        )
        if looks_login_url(response.url) or looks_logged_out_html(response.text):
            raise CommandError(
                code="AUTH_EXPIRED",
                message="Saved KLMS auth did not stay authenticated while loading course contents.",
                hint="Run `kaist klms auth refresh` and try again.",
                exit_code=10,
                retryable=True,
            )
        return _unwrap_moodle_ajax_payload(response.text)

    def _resolve_target_item(
        self,
        *,
        context: Any,
        config: KlmsConfig,
        auth_mode: str,
        target: str,
    ) -> FileItem:
        items = self._list_html(
            context=context,
            config=config,
            auth_mode=auth_mode,
            course_id=None,
            course_query=None,
            limit=None,
        )
        normalized_url = abs_url(config.base_url, target) if _looks_like_url_candidate(target) else None
        selected = None
        if normalized_url is not None:
            for item in items:
                if item.url == normalized_url or item.download_url == normalized_url:
                    selected = item
                    break
            if selected is None:
                if not _looks_like_material_target_url(normalized_url):
                    raise CommandError(
                        code="CONFIG_INVALID",
                        message=f"Not a KLMS file/material URL: {target}",
                        hint="Pass a direct file URL or a file/module ID returned by `kaist klms files list`.",
                        exit_code=40,
                    )
                selected = _synthesize_file_item_from_url(
                    normalized_url,
                    course_id=None,
                    course_title=None,
                    course_code=None,
                    auth_mode=auth_mode,
                )
        else:
            for item in items:
                if str(item.id or "") == target:
                    selected = item
                    break
        if selected is None:
            raise CommandError(
                code="NOT_FOUND",
                message=f"File not found: {target}",
                hint="Pass a file URL or a file/module ID returned by `kaist klms files list`.",
                exit_code=44,
            )
        return self._resolve_item(context=context, config=config, item=selected)

    def _resolve_item(self, *, context: Any, config: KlmsConfig, item: FileItem) -> FileItem:
        if item.download_url and _looks_like_direct_file_url(item.download_url):
            return replace(
                item,
                filename=item.filename or _filename_from_url(item.download_url),
                downloadable=True,
                confidence=max(item.confidence, 0.84 if str(item.source or "").startswith("api:") else 0.76),
                source="api:file-resolved" if str(item.source or "").startswith("api:") else "html:file-resolved",
            )
        if not item.url:
            return item
        if _looks_like_direct_file_url(item.url):
            return replace(
                item,
                download_url=item.download_url or item.url,
                filename=item.filename or _filename_from_url(item.url),
                downloadable=True,
                confidence=max(item.confidence, 0.62),
            )

        page = context.new_page()
        html = ""
        final_url = item.url
        try:
            try:
                page.goto(item.url, wait_until="domcontentloaded", timeout=30_000)
                html = page.content()
                final_url = page.url
            except Exception as exc:
                if "Download is starting" not in str(exc):
                    raise
                return replace(
                    item,
                    download_url=item.download_url or item.url,
                    downloadable=True,
                    confidence=max(item.confidence, 0.72),
                    source="html:file-resolved",
                )
        finally:
            page.close()

        if looks_login_url(final_url) or looks_logged_out_html(html):
            raise CommandError(
                code="AUTH_EXPIRED",
                message="Saved KLMS auth did not reach the file page.",
                hint="Run `kaist klms auth refresh` and try again.",
                exit_code=10,
                retryable=True,
            )
        if error_text := looks_klms_error_html(html):
            raise CommandError(
                code="NOT_FOUND",
                message=f"File not found: {item.id or item.url or item.title}",
                hint=f"KLMS returned an error page while resolving the file target: {error_text}",
                exit_code=44,
            )

        title = item.title
        page_title = _extract_material_title_from_page(html)
        if page_title and (not title or title.startswith("material-")):
            title = page_title

        download_url = item.download_url
        filename = item.filename
        downloadable = item.downloadable
        if _looks_like_direct_file_url(final_url):
            download_url = final_url
            filename = filename or _filename_from_url(final_url)
            downloadable = True
        elif item.kind == "file" and download_url is None:
            download_url = item.url

        return replace(
            item,
            title=title,
            download_url=download_url,
            filename=filename,
            downloadable=downloadable,
            confidence=max(item.confidence, 0.76 if download_url else 0.68),
            source="html:file-resolved" if download_url else item.source,
        )

    def _download_resolved_item(
        self,
        *,
        context: Any,
        config: KlmsConfig,
        item: FileItem,
        filename_override: str | None,
        subdir: str | None,
        dest: str | None,
        if_exists: str,
        auth_mode: str,
    ) -> dict[str, Any]:
        download_url = str(item.download_url or item.url or "").strip()
        if not download_url:
            raise CommandError(code="NOT_DOWNLOADABLE", message="No download URL was available for this item.", exit_code=40)
        if _looks_like_video_item(item.title, download_url) or (item.filename and _is_video_filename(item.filename)):
            raise CommandError(
                code="NOT_DOWNLOADABLE",
                message=f"Refusing to download a video-like item via files download: {item.title}",
                hint="This belongs in the future `videos` surface, not the files downloader.",
                exit_code=40,
            )

        out_dir = _resolve_destination_root(files_root=self._paths.files_root, subdir=subdir, dest=dest)

        final_name = str(filename_override or item.filename or _filename_from_url(download_url) or f"download-{item.id or 'file'}").strip()
        out_path = out_dir / final_name
        if out_path.exists() and if_exists == "skip":
            return {
                "skipped": True,
                "reason": "exists",
                "path": str(out_path),
                "filename": final_name,
                "download_url": download_url,
                "auth_mode": auth_mode,
                "item": item.to_dict(),
            }

        if _looks_like_direct_file_url(download_url):
            http = KlmsHttpSession(context, base_url=config.base_url)
            try:
                http_result = http.download_to_path(
                    download_url,
                    destination=out_path,
                    timeout_seconds=180.0,
                )
                return {
                    "ok": True,
                    "path": http_result.path,
                    "filename": final_name,
                    "download_url": http_result.url,
                    "auth_mode": auth_mode,
                    "transport": "http",
                    "bytes_written": http_result.bytes_written,
                    "item": item.to_dict(),
                }
            except KlmsDownloadFallback:
                if out_path.exists():
                    out_path.unlink(missing_ok=True)
            except Exception:
                if out_path.exists():
                    out_path.unlink(missing_ok=True)
                raise

        page = context.new_page()
        try:
            try:
                with page.expect_download() as download_info:
                    try:
                        page.goto(download_url, wait_until="commit", timeout=30_000)
                    except Exception as exc:
                        if "Download is starting" not in str(exc):
                            raise
                download = download_info.value
            finally:
                pass
        finally:
            page.close()

        suggested_name = str(download.suggested_filename or "").strip() or None
        browser_name = str(filename_override or suggested_name or item.filename or _filename_from_url(download_url) or f"download-{item.id or 'file'}").strip()
        browser_path = out_dir / browser_name
        if browser_path.exists() and if_exists == "skip":
            return {
                "skipped": True,
                "reason": "exists",
                "path": str(browser_path),
                "filename": browser_name,
                "download_url": download_url,
                "auth_mode": auth_mode,
                "item": item.to_dict(),
            }
        download.save_as(str(browser_path))
        return {
            "ok": True,
            "path": str(browser_path),
            "filename": browser_name,
            "download_url": download_url,
            "auth_mode": auth_mode,
            "transport": "browser",
            "item": item.to_dict(),
        }

    def download_item_with_context(
        self,
        *,
        context: Any,
        config: KlmsConfig,
        item: FileItem,
        filename_override: str | None = None,
        subdir: str | None = None,
        dest: str | None = None,
        if_exists: str = "skip",
        auth_mode: str,
    ) -> dict[str, Any]:
        return self._download_resolved_item(
            context=context,
            config=config,
            item=item,
            filename_override=filename_override,
            subdir=subdir,
            dest=dest,
            if_exists=if_exists,
            auth_mode=auth_mode,
        )
