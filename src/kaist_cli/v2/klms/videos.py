from __future__ import annotations

import re
from dataclasses import replace
from typing import Any
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup  # type: ignore[import-untyped]

from ..contracts import CommandError, CommandResult
from .auth import AuthService, looks_logged_out_html, looks_login_url
from .config import KlmsConfig, abs_url, load_config
from .courses import _course_code_base, _course_is_current_term, _course_matches_query, _discover_courses_from_dashboard, _extract_current_term_from_dashboard, _is_noise_course, _norm_text
from .models import Video
from .paths import KlmsPaths
from .session import KlmsSessionBootstrap, build_session_bootstrap, fetch_html_batch
from .validate import looks_klms_error_html

MAX_VIDEO_HTTP_WORKERS = 4


def _extract_video_id_from_url(url: str) -> str | None:
    parsed = urlparse(url)
    if "/mod/vod/" not in (parsed.path or ""):
        return None
    query = parse_qs(parsed.query, keep_blank_values=True)
    value = (query.get("id") or [None])[0]
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _simplify_video_title(text: str) -> str:
    title = _norm_text(text)
    if not title:
        return ""
    title = re.sub(r"\s+VOD\s*$", "", title, flags=re.IGNORECASE).strip()
    if " : " in title:
        head, tail = title.split(" : ", 1)
        if head.strip() and tail.strip():
            return tail.strip()
    return title


def _course_map_from_dashboard(html: str, *, base_url: str, configured_ids: tuple[str, ...]) -> dict[str, dict[str, str | None]]:
    courses = {
        str(course.id): {
            "course_id": str(course.id),
            "course_title": course.title,
            "course_code": course.course_code,
            "term_label": course.term_label,
        }
        for course in _discover_courses_from_dashboard(html, base_url=base_url)
    }
    for configured_id in configured_ids:
        course_id = str(configured_id).strip()
        if not course_id:
            continue
        courses.setdefault(course_id, {"course_id": course_id, "course_title": None, "course_code": None, "term_label": None})
    return courses


def _merge_videos(items: list[Video]) -> list[Video]:
    merged: dict[str, Video] = {}
    for item in items:
        key = str(item.id or item.url or "")
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
            title=winner.title if len(winner.title) >= len(loser.title) else loser.title,
            url=winner.url or loser.url,
            viewer_url=winner.viewer_url or loser.viewer_url,
            stream_url=winner.stream_url or loser.stream_url,
            course_id=winner.course_id or loser.course_id,
            course_title=winner.course_title or loser.course_title,
            course_code=winner.course_code or loser.course_code,
            course_code_base=winner.course_code_base or loser.course_code_base,
            source=winner.source if winner.source == loser.source else "mixed:vod-surface",
            confidence=max(winner.confidence, loser.confidence),
            auth_mode=winner.auth_mode or loser.auth_mode,
        )
    return list(merged.values())


def _extract_video_items_from_html(
    html: str,
    *,
    base_url: str,
    course_id: str | None,
    course_title: str | None,
    course_code: str | None,
    auth_mode: str | None,
    source: str,
) -> list[Video]:
    soup = BeautifulSoup(html, "html.parser")
    out: list[Video] = []

    def in_header_like_region(element: Any) -> bool:
        current = element
        for _ in range(12):
            if not current or not getattr(current, "attrs", None):
                break
            classes = " ".join(current.attrs.get("class", [])).lower()
            if any(
                marker in classes
                for marker in ("ks-header", "all-menu", "tooltip-layer", "breadcrumb", "navbar", "footer", "menu")
            ):
                return True
            current = current.parent
        return False

    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href") or "").strip()
        if "/mod/vod/view.php" not in href:
            continue
        if anchor.get("target") == "_blank" or in_header_like_region(anchor):
            continue
        video_url = abs_url(base_url, href)
        video_id = _extract_video_id_from_url(video_url)
        if not video_id:
            continue
        title = _simplify_video_title(
            anchor.get_text(" ", strip=True)
            or str(anchor.get("title") or anchor.get("aria-label") or "")
            or f"vod-{video_id}"
        )
        if not title or title.lower() in {"watch vod", "vod"}:
            continue
        out.append(
            Video(
                id=video_id,
                title=title,
                url=video_url,
                viewer_url=None,
                stream_url=None,
                course_id=course_id,
                course_title=course_title,
                course_code=course_code,
                course_code_base=_course_code_base(course_code),
                source=source,
                confidence=0.78 if source == "html:course-view" else 0.72,
                auth_mode=auth_mode,
            )
        )

    return _merge_videos(out)


def _parse_video_detail_from_html(html: str, *, base_url: str, fallback_id: str | None) -> dict[str, str | None]:
    soup = BeautifulSoup(html, "html.parser")
    title: str | None = None
    for selector in ("#page-header h1", ".page-header-headings h1", "h1", "title"):
        node = soup.select_one(selector)
        if not node:
            continue
        candidate = _simplify_video_title(node.get_text(" ", strip=True))
        if candidate:
            title = candidate
            break

    viewer_url: str | None = None
    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href") or "").strip()
        if "/mod/vod/viewer/index.php" not in href:
            continue
        viewer_url = abs_url(base_url, href)
        break
    if viewer_url is None and fallback_id:
        viewer_url = abs_url(base_url, f"/mod/vod/viewer/index.php?id={fallback_id}")

    return {"title": title, "viewer_url": viewer_url}


def _parse_video_viewer_from_html(html: str, *, base_url: str) -> dict[str, str | None]:
    soup = BeautifulSoup(html, "html.parser")
    title: str | None = None
    for selector in ("#page-header h1", ".page-header-headings h1", "h1", "title"):
        node = soup.select_one(selector)
        if not node:
            continue
        candidate = _simplify_video_title(node.get_text(" ", strip=True))
        if candidate:
            title = candidate
            break

    stream_url: str | None = None
    for selector in ("video[src]", "source[src]", "iframe[src]"):
        node = soup.select_one(selector)
        if not node:
            continue
        src = str(node.get("src") or "").strip()
        if src:
            stream_url = abs_url(base_url, src)
            break
    if stream_url is None:
        match = re.search(r"""src\s*:\s*["']([^"']+\.(?:mp4|m3u8)(?:\?[^"']*)?)["']""", html, flags=re.IGNORECASE)
        if match:
            stream_url = abs_url(base_url, match.group(1))
    return {"title": title, "stream_url": stream_url}


class VideoService:
    def __init__(self, paths: KlmsPaths, auth: AuthService) -> None:
        self._paths = paths
        self._auth = auth

    def list_with_context(
        self,
        *,
        context: Any,
        config: KlmsConfig,
        auth_mode: str,
        course_id: str | None = None,
        course_query: str | None = None,
        limit: int | None = None,
        recent: bool = False,
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
            recent=recent,
            bootstrap=bootstrap,
        )
        return CommandResult(data=[item.to_dict() for item in items], source="html", capability="partial")

    def list(
        self,
        *,
        course_id: str | None = None,
        course_query: str | None = None,
        limit: int | None = None,
        recent: bool = False,
    ) -> CommandResult:
        config = load_config(self._paths)

        def callback(context: Any, auth_mode: str) -> CommandResult:
            return self.list_with_context(
                context=context,
                config=config,
                auth_mode=auth_mode,
                course_id=course_id,
                course_query=course_query,
                limit=limit,
                recent=recent,
            )

        return self._auth.run_authenticated(
            config=config,
            headless=True,
            accept_downloads=False,
            timeout_seconds=10.0,
            callback=callback,
        )

    def show(self, video_id_or_url: str, *, course_id_hint: str | None = None) -> CommandResult:
        config = load_config(self._paths)
        target = str(video_id_or_url).strip()
        if not target:
            raise CommandError(code="CONFIG_INVALID", message="Video ID or URL is required.", exit_code=40)

        def callback(context: Any, auth_mode: str) -> CommandResult:
            resolved = self._resolve_target_video(
                context=context,
                config=config,
                auth_mode=auth_mode,
                target=target,
                course_id_hint=course_id_hint,
            )
            return CommandResult(data=resolved.to_dict(), source="html", capability="partial")

        return self._auth.run_authenticated(
            config=config,
            headless=True,
            accept_downloads=False,
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
        recent: bool,
        bootstrap: KlmsSessionBootstrap,
    ) -> list[Video]:
        course_map = self._course_map_for_request(
            bootstrap=bootstrap,
            config=config,
            course_id=course_id,
            course_query=course_query,
        )
        if not course_map:
            return []

        items: list[Video] = []
        course_meta_list = list(course_map.values())
        for start in range(0, len(course_meta_list), MAX_VIDEO_HTTP_WORKERS):
            batch = course_meta_list[start : start + MAX_VIDEO_HTTP_WORKERS]
            course_paths: list[str] = []
            path_to_course: dict[str, dict[str, str | None]] = {}
            for course_meta in batch:
                current_course_id = str(course_meta.get("course_id") or "").strip() or None
                if not current_course_id:
                    continue
                path = f"/course/view.php?id={current_course_id}&section=0"
                course_paths.append(path)
                path_to_course[path] = course_meta

            responses = fetch_html_batch(bootstrap.http, course_paths, max_workers=MAX_VIDEO_HTTP_WORKERS)
            index_paths: list[str] = []
            index_to_course: dict[str, dict[str, str | None]] = {}
            for path in course_paths:
                response = responses.get(path)
                if response is None:
                    continue
                if looks_login_url(response.url) or looks_logged_out_html(response.text):
                    raise CommandError(
                        code="AUTH_EXPIRED",
                        message="Saved KLMS auth did not stay authenticated while loading course video pages.",
                        hint="Run `kaist klms auth refresh` and try again.",
                        exit_code=10,
                        retryable=True,
                    )
                course_meta = path_to_course[path]
                current_course_id = str(course_meta.get("course_id") or "").strip() or None
                current_course_title = str(course_meta.get("course_title") or "").strip() or None
                current_course_code = str(course_meta.get("course_code") or "").strip() or None
                extracted = _extract_video_items_from_html(
                    response.text,
                    base_url=config.base_url,
                    course_id=current_course_id,
                    course_title=current_course_title,
                    course_code=current_course_code,
                    auth_mode=auth_mode,
                    source="html:course-view",
                )
                items.extend(extracted)
                if not extracted and f"/mod/vod/index.php?id={current_course_id}" in response.text and current_course_id:
                    index_path = f"/mod/vod/index.php?id={current_course_id}"
                    index_paths.append(index_path)
                    index_to_course[index_path] = course_meta
                if limit is not None and len(_merge_videos(items)) >= limit:
                    break
            if limit is not None and len(_merge_videos(items)) >= limit:
                break

            if index_paths:
                index_responses = fetch_html_batch(bootstrap.http, index_paths, max_workers=MAX_VIDEO_HTTP_WORKERS)
                for path in index_paths:
                    response = index_responses.get(path)
                    if response is None:
                        continue
                    if looks_login_url(response.url) or looks_logged_out_html(response.text):
                        raise CommandError(
                            code="AUTH_EXPIRED",
                            message="Saved KLMS auth did not stay authenticated while loading VOD indexes.",
                            hint="Run `kaist klms auth refresh` and try again.",
                            exit_code=10,
                            retryable=True,
                        )
                    course_meta = index_to_course[path]
                    items.extend(
                        _extract_video_items_from_html(
                            response.text,
                            base_url=config.base_url,
                            course_id=str(course_meta.get("course_id") or "").strip() or None,
                            course_title=str(course_meta.get("course_title") or "").strip() or None,
                            course_code=str(course_meta.get("course_code") or "").strip() or None,
                            auth_mode=auth_mode,
                            source="html:vod-index",
                        )
                    )
                    if limit is not None and len(_merge_videos(items)) >= limit:
                        break
            if limit is not None and len(_merge_videos(items)) >= limit:
                break

        deduped = _merge_videos(items)
        if recent:
            deduped.sort(key=lambda item: int(str(item.id or "0").strip() or "0"), reverse=True)
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
        current_term_label = _extract_current_term_from_dashboard(bootstrap.dashboard_html)
        if course_id:
            target = str(course_id).strip()
            course_meta = course_map.get(target) or {"course_id": target, "course_title": None, "course_code": None, "term_label": None}
            return {target: course_meta}
        return {
            key: value
            for key, value in course_map.items()
            if not _is_noise_course(str(value.get("course_title") or ""), config.exclude_course_title_patterns)
            and _course_matches_query(
                type(
                    "_CourseQueryCarrier",
                    (),
                    {
                        "title": str(value.get("course_title") or ""),
                        "course_code": value.get("course_code"),
                        "course_code_base": _course_code_base(str(value.get("course_code") or "").strip() or None),
                    },
                ),
                course_query,
            )
            and _course_is_current_term(
                type(
                    "_CourseTermCarrier",
                    (),
                    {
                        "title": str(value.get("course_title") or ""),
                        "course_code": value.get("course_code"),
                        "course_code_base": _course_code_base(str(value.get("course_code") or "").strip() or None),
                        "term_label": value.get("term_label"),
                    },
                ),
                current_term_label,
                include_past=False,
            )
        }

    def _resolve_target_video(
        self,
        *,
        context: Any,
        config: KlmsConfig,
        auth_mode: str,
        target: str,
        course_id_hint: str | None,
    ) -> Video:
        bootstrap = build_session_bootstrap(
            self._paths,
            context=context,
            config=config,
            auth_mode=auth_mode,
        )
        selected: Video | None = None
        if target.startswith(("http://", "https://", "/")):
            normalized = abs_url(config.base_url, target)
            video_id = _extract_video_id_from_url(normalized)
            selected = Video(
                id=video_id,
                title=f"vod-{video_id}" if video_id else "video",
                url=normalized,
                viewer_url=None,
                stream_url=None,
                course_id=str(course_id_hint).strip() or None if course_id_hint else None,
                course_title=None,
                course_code=None,
                course_code_base=_course_code_base(str(course_id_hint).strip()) if course_id_hint else None,
                source="url:synthetic",
                confidence=0.35,
                auth_mode=auth_mode,
            )
        else:
            items = self._list_html(
                context=context,
                config=config,
                auth_mode=auth_mode,
                course_id=course_id_hint,
                course_query=None,
                limit=None,
                recent=False,
                bootstrap=bootstrap,
            )
            for item in items:
                if str(item.id or "") == target:
                    selected = item
                    break
            if selected is None:
                selected = Video(
                    id=target,
                    title=f"vod-{target}",
                    url=abs_url(config.base_url, f"/mod/vod/view.php?id={target}"),
                    viewer_url=None,
                    stream_url=None,
                    course_id=str(course_id_hint).strip() or None if course_id_hint else None,
                    course_title=None,
                    course_code=None,
                    course_code_base=_course_code_base(str(course_id_hint).strip()) if course_id_hint else None,
                    source="url:synthetic",
                    confidence=0.3,
                    auth_mode=auth_mode,
                )
        return self._resolve_video_detail(context=context, config=config, bootstrap=bootstrap, item=selected)

    def _resolve_video_detail(
        self,
        *,
        context: Any,
        config: KlmsConfig,
        bootstrap: KlmsSessionBootstrap,
        item: Video,
    ) -> Video:
        detail_url = str(item.url or "").strip() or abs_url(config.base_url, f"/mod/vod/view.php?id={item.id}")
        detail_response = bootstrap.http.get_html(detail_url, context=context, timeout_seconds=20.0)
        if looks_login_url(detail_response.url) or looks_logged_out_html(detail_response.text):
            raise CommandError(
                code="AUTH_EXPIRED",
                message="Saved KLMS auth did not reach the video page.",
                hint="Run `kaist klms auth refresh` and try again.",
                exit_code=10,
                retryable=True,
            )
        if error_text := looks_klms_error_html(detail_response.text):
            raise CommandError(
                code="NOT_FOUND",
                message=f"Video not found: {item.id or detail_url}",
                hint=f"KLMS returned an error page for the video target: {error_text}",
                exit_code=44,
            )
        detail = _parse_video_detail_from_html(detail_response.text, base_url=config.base_url, fallback_id=item.id)
        viewer_url = detail.get("viewer_url") or item.viewer_url
        title = detail.get("title") or item.title
        source = "html:vod-detail"
        confidence = max(item.confidence, 0.8)
        stream_url = item.stream_url
        if viewer_url:
            viewer_response = bootstrap.http.get_html(viewer_url, context=context, timeout_seconds=20.0)
            if not looks_login_url(viewer_response.url) and not looks_logged_out_html(viewer_response.text):
                if error_text := looks_klms_error_html(viewer_response.text):
                    raise CommandError(
                        code="NOT_FOUND",
                        message=f"Video not found: {item.id or viewer_url}",
                        hint=f"KLMS returned an error page for the VOD viewer: {error_text}",
                        exit_code=44,
                    )
                viewer = _parse_video_viewer_from_html(viewer_response.text, base_url=config.base_url)
                title = viewer.get("title") or title
                stream_url = viewer.get("stream_url") or stream_url
                source = "html:vod-viewer" if stream_url else source
                confidence = max(confidence, 0.88 if stream_url else 0.82)

        return replace(
            item,
            title=title,
            url=detail_url,
            viewer_url=viewer_url,
            stream_url=stream_url,
            source=source,
            confidence=confidence,
        )
