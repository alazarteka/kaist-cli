from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from bs4 import BeautifulSoup  # type: ignore[import-untyped]

from ..contracts import CommandError, CommandResult
from .auth import AuthService, extract_sesskey, looks_logged_out_html, looks_login_url
from .config import KlmsConfig, abs_url, load_config
from .courses import (
    _course_code_base,
    _course_is_current_term,
    _course_matches_query,
    _discover_courses_from_dashboard,
    _extract_current_term_from_dashboard,
    _is_noise_course,
    _norm_text,
    _select_dashboard_courses,
    _term_label_from_course_code,
)
from .deadline import RefreshDeadline
from .discovery import load_json_summary
from .models import Assignment
from .paths import KlmsPaths
from .provider_state import ProviderLoad
from .session import KlmsSessionBootstrap, build_session_bootstrap
from .validate import looks_klms_error_html


KAIST_LOCAL_TZ = timezone(timedelta(hours=9))


def _strip_html_text(raw: str | None) -> str | None:
    text = str(raw or "").strip()
    if not text:
        return None
    normalized = _norm_text(BeautifulSoup(text, "html.parser").get_text(" ", strip=True))
    normalized = re.sub(r"\s+([,.:;])", r"\1", normalized)
    return normalized or None


def _parse_datetime_guess(raw: str) -> str | None:
    text = _norm_text(raw)
    if not text:
        return None

    moodle_raw = re.sub(r"^[A-Za-z]+,\s*", "", text).replace(",", "")
    for fmt in ("%d %B %Y %I:%M %p", "%d %B %Y %H:%M"):
        try:
            dt = datetime.strptime(moodle_raw, fmt).replace(tzinfo=KAIST_LOCAL_TZ)
            return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        except ValueError:
            pass

    for fmt in (
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%Y/%m/%d %H:%M",
        "%Y/%m/%d",
        "%Y.%m.%d %H:%M",
        "%Y.%m.%d",
    ):
        try:
            dt = datetime.strptime(text, fmt).replace(tzinfo=KAIST_LOCAL_TZ)
            return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        except ValueError:
            continue
    return None


def _iso_from_epoch(epoch: float | int | None) -> str | None:
    if not isinstance(epoch, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(float(epoch), tz=timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    except Exception:
        return None


def _iso_now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _epoch_from_iso(value: str | None) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    normalized = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _discover_course_ids_from_dashboard(html: str, *, configured_ids: tuple[str, ...]) -> list[str]:
    out: list[str] = [str(course_id).strip() for course_id in configured_ids if str(course_id).strip()]
    out.extend(re.findall(r"/course/view\.php\?id=(\d+)", html))
    seen: set[str] = set()
    deduped: list[str] = []
    for value in out:
        if not value or value in seen:
            continue
        seen.add(value)
        deduped.append(value)
    return deduped


def _discover_current_term_course_ids_from_dashboard(
    html: str,
    *,
    base_url: str,
    configured_ids: tuple[str, ...],
    exclude_patterns: tuple[str, ...],
    include_past: bool,
) -> list[str]:
    discovered = _select_dashboard_courses(
        html,
        base_url=base_url,
        exclude_patterns=exclude_patterns,
        course_query=None,
        include_past=include_past,
        allow_termless_fallback=True,
    )
    out = [str(course.id).strip() for course in discovered if str(course.id).strip()]
    if include_past:
        out.extend(str(course_id).strip() for course_id in configured_ids if str(course_id).strip())
    seen: set[str] = set()
    deduped: list[str] = []
    for value in out:
        if not value or value in seen:
            continue
        seen.add(value)
        deduped.append(value)
    return deduped


def _assignment_matches_course_query(assignment: Assignment, query: str | None) -> bool:
    needle = _norm_text(str(query or "")).lower()
    if not needle:
        return True
    haystacks = (
        assignment.course_title,
        assignment.course_code,
        assignment.course_code_base,
    )
    return any(needle in _norm_text(value or "").lower() for value in haystacks if value)


def _assignment_is_current_term(assignment: Assignment, current_term_label: str | None, *, include_past: bool) -> bool:
    if include_past or not current_term_label:
        return True
    synthetic = type(
        "_CourseTermCarrier",
        (),
        {
            "title": assignment.course_title or "",
            "course_code": assignment.course_code,
            "course_code_base": assignment.course_code_base,
            "term_label": _term_label_from_course_code(assignment.course_code),
        },
    )
    return _course_is_current_term(synthetic, current_term_label, include_past=include_past)


def _recommended_methodnames(paths: KlmsPaths, *categories: str) -> list[str]:
    api_map = load_json_summary(str(paths.api_map_path)) or {}
    allowed = {str(category) for category in categories}
    out: list[str] = []
    for endpoint in api_map.get("recommended_endpoints") or []:
        if not isinstance(endpoint, dict):
            continue
        if str(endpoint.get("category") or "") not in allowed:
            continue
        methodname = endpoint.get("methodname")
        if isinstance(methodname, str) and methodname.strip():
            out.append(methodname.strip())
    return list(dict.fromkeys(out))


def _extract_assignment_rows_from_calendar_data(
    data: Any,
    *,
    base_url: str,
    auth_mode: str,
) -> list[Assignment]:
    candidates: list[dict[str, Any]] = []

    def push_list(items: Any) -> None:
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict):
                    candidates.append(item)

    if isinstance(data, dict):
        push_list(data.get("events"))
        push_list(data.get("data"))
        push_list(data.get("items"))
    elif isinstance(data, list):
        push_list(data)

    assignments: list[Assignment] = []
    for row in candidates:
        module = str(row.get("modulename") or row.get("modname") or "").lower()
        eventtype = str(row.get("eventtype") or row.get("name") or "").lower()
        if module and module != "assign":
            continue
        if "assign" not in module and "assignment" not in eventtype and "assign" not in eventtype:
            continue

        course = row.get("course") if isinstance(row.get("course"), dict) else {}
        course_id = str(row.get("courseid") or row.get("course_id") or course.get("id") or "").strip() or None
        course_title = _norm_text(str(course.get("fullname") or course.get("fullnamedisplay") or "")) or None
        course_code = str(course.get("shortname") or "").strip() or None
        title = _norm_text(str(row.get("name") or row.get("title") or "assignment"))
        title = re.sub(r"\s+is due$", "", title, flags=re.IGNORECASE).strip() or title
        due_raw = _strip_html_text(str(row.get("formattedtime") or row.get("timestring") or "")) or None
        due_iso = _iso_from_epoch(row.get("timesort") or row.get("timestart") or row.get("timedue"))
        if due_iso is None and due_raw:
            due_iso = _parse_datetime_guess(due_raw)
        url = row.get("url") or row.get("viewurl") or row.get("view_url")
        assignments.append(
            Assignment(
                id=str(row.get("instance") or row.get("id") or "").strip() or None,
                title=title,
                url=abs_url(base_url, str(url)) if isinstance(url, str) and url.strip() else None,
                due_raw=due_raw,
                due_iso=due_iso,
                course_id=course_id,
                course_title=course_title,
                course_code=course_code,
                course_code_base=_course_code_base(course_code),
                source="api:core_calendar_get_action_events_by_timesort",
                confidence=0.86,
                auth_mode=auth_mode,
            )
        )
    return assignments


def _filter_assignments(
    assignments: list[Assignment],
    *,
    course_id: str | None,
    course_query: str | None,
    since_iso: str | None,
    limit: int | None,
    current_term_label: str | None = None,
    current_term_course_ids: set[str] | None = None,
    include_past: bool = False,
) -> list[Assignment]:
    filtered = assignments
    if course_id:
        target = str(course_id).strip()
        filtered = [assignment for assignment in filtered if str(assignment.course_id or "").strip() == target]
    if course_query:
        filtered = [assignment for assignment in filtered if _assignment_matches_course_query(assignment, course_query)]
    if not include_past and current_term_course_ids is not None:
        filtered = [
            assignment
            for assignment in filtered
            if str(assignment.course_id or "").strip() in current_term_course_ids
            or (
                not str(assignment.course_id or "").strip()
                and _assignment_is_current_term(assignment, current_term_label, include_past=include_past)
            )
        ]
    else:
        filtered = [
            assignment
            for assignment in filtered
            if _assignment_is_current_term(assignment, current_term_label, include_past=include_past)
        ]
    if since_iso:
        floor = str(since_iso).strip()
        filtered = [assignment for assignment in filtered if assignment.due_iso and assignment.due_iso >= floor]
    filtered = sorted(
        filtered,
        key=lambda assignment: (
            assignment.due_iso is None,
            assignment.due_iso or "",
            assignment.course_title or "",
            assignment.title,
        ),
    )
    if limit is not None:
        filtered = filtered[: max(0, limit)]
    return filtered


def _extract_assignments_from_index_html(
    html: str,
    *,
    base_url: str,
    course_id: str,
) -> list[Assignment]:
    soup = BeautifulSoup(html, "html.parser")
    out: list[Assignment] = []

    def find_table_with_due_headers() -> tuple[list[str], Any] | None:
        for table in soup.find_all("table"):
            rows = table.find_all("tr")
            if not rows:
                continue
            headers = [_norm_text(cell.get_text(" ", strip=True)) for cell in rows[0].find_all(["th", "td"])]
            headers_norm = [header.lower() for header in headers]
            if any(any(needle in header for needle in ("due", "마감", "기한", "종료")) for header in headers_norm):
                return headers, table
        return None

    found = find_table_with_due_headers()
    if not found:
        for anchor in soup.find_all("a", href=True):
            href = str(anchor["href"])
            if "mod/assign/view.php" not in href:
                continue
            title = _norm_text(anchor.get_text(" ", strip=True)) or "assignment"
            match = re.search(r"[?&]id=(\d+)", href)
            out.append(
                Assignment(
                    id=match.group(1) if match else None,
                    title=title,
                    url=abs_url(base_url, href),
                    due_raw=None,
                    due_iso=None,
                    course_id=course_id,
                    course_title=None,
                    course_code=None,
                    course_code_base=None,
                    source="html:assign-index-fallback",
                    confidence=0.62,
                )
            )
        return out

    headers, table = found
    headers_norm = [header.lower() for header in headers]

    def col_index(*needles: str) -> int | None:
        for needle in needles:
            for index, header in enumerate(headers_norm):
                if needle in header:
                    return index
        return None

    name_i = col_index("assignment", "과제", "과제명", "name", "제목") or 0
    due_i = col_index("due", "마감", "기한", "종료")
    rows = table.find_all("tr")
    if rows and rows[0].find_all("th"):
        rows = rows[1:]

    for row in rows:
        cells = row.find_all(["td", "th"])
        if not cells or name_i >= len(cells):
            continue
        name_cell = cells[name_i]
        link = name_cell.find("a", href=True)
        title = _norm_text(name_cell.get_text(" ", strip=True)) or "assignment"
        href = str(link["href"]) if link else None
        due_raw = None
        if due_i is not None and due_i < len(cells):
            due_raw = _norm_text(cells[due_i].get_text(" ", strip=True)) or None
        match = re.search(r"[?&]id=(\d+)", href or "")
        out.append(
            Assignment(
                id=match.group(1) if match else None,
                title=title,
                url=abs_url(base_url, href) if href else None,
                due_raw=due_raw,
                due_iso=_parse_datetime_guess(due_raw) if due_raw else None,
                course_id=course_id,
                course_title=None,
                course_code=None,
                course_code_base=None,
                source="html:assign-index",
                confidence=0.68,
            )
        )
    return out


def _split_assignment_title_context(raw: str, *, assignment_id: str) -> tuple[str, str | None]:
    title = _norm_text(raw)
    if not title:
        return f"assignment-{assignment_id}", None
    title = re.sub(r"^\s*Assignment:\s*", "", title, flags=re.IGNORECASE).strip()
    match = re.match(r"^[A-Z]{2,}\.[A-Z0-9()_-]+_20\d{2}_\d+\s*:\s*(.+)$", title)
    if match:
        cleaned = _norm_text(match.group(1))
        if cleaned:
            code_match = re.match(r"^([A-Z]{2,}\.[A-Z0-9()_-]+_20\d{2}_\d+)\s*:", title)
            return cleaned, code_match.group(1) if code_match else None
    return title, None


def _extract_course_context_from_assignment_page(soup: BeautifulSoup) -> tuple[str | None, str | None]:
    selector_order = (
        "#page-navbar a[href*='course/view.php']",
        ".breadcrumb a[href*='course/view.php']",
        "nav a[href*='course/view.php']",
        ".page-context-header a[href*='course/view.php']",
        "a[href*='course/view.php']",
    )
    candidates: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    for selector in selector_order:
        for anchor in soup.select(selector):
            href = str(anchor.get("href") or "")
            match = re.search(r"[?&]id=(\d+)", href)
            if not match:
                continue
            course_id = match.group(1)
            course_title = _norm_text(anchor.get_text(" ", strip=True))
            if not course_title:
                continue
            pair = (course_id, course_title)
            if pair in seen:
                continue
            seen.add(pair)
            candidates.append(pair)

    for course_id, course_title in candidates:
        if course_title.lower() in {"course home", "course contents", "home"}:
            continue
        if not _is_noise_course(course_title, ()):
            return course_id, course_title
    return candidates[0] if candidates else (None, None)


class AssignmentService:
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
        since_iso: str | None = None,
        limit: int | None = None,
        include_past: bool = False,
        bootstrap: KlmsSessionBootstrap | None = None,
    ) -> CommandResult:
        bootstrap = bootstrap or build_session_bootstrap(
            self._paths,
            context=context,
            config=config,
            auth_mode=auth_mode,
        )
        api_assignments = self._list_api(
            context=context,
            config=config,
            auth_mode=auth_mode,
            course_id=course_id,
            course_query=course_query,
            since_iso=since_iso,
            limit=limit,
            include_past=include_past,
            bootstrap=bootstrap,
        )
        if api_assignments is not None:
            return CommandResult(
                data=[assignment.to_dict() for assignment in api_assignments],
                source="moodle_ajax",
                capability="full",
            )

        html_assignments = self._list_html(
            context=context,
            config=config,
            course_id=course_id,
            course_query=course_query,
            since_iso=since_iso,
            limit=limit,
            include_past=include_past,
            bootstrap=bootstrap,
        )
        return CommandResult(
            data=[assignment.to_dict() for assignment in html_assignments],
            source="html",
            capability="partial",
        )

    def list(
        self,
        *,
        course_id: str | None = None,
        course_query: str | None = None,
        since_iso: str | None = None,
        limit: int | None = None,
        include_past: bool = False,
    ) -> CommandResult:
        config = load_config(self._paths)

        def callback(context: Any, auth_mode: str) -> CommandResult:
            return self.list_with_context(
                context=context,
                config=config,
                auth_mode=auth_mode,
                course_id=course_id,
                course_query=course_query,
                since_iso=since_iso,
                limit=limit,
                include_past=include_past,
            )

        return self._auth.run_authenticated(
            config=config,
            headless=True,
            accept_downloads=False,
            timeout_seconds=10.0,
            callback=callback,
        )

    def load_for_dashboard(
        self,
        *,
        context: Any,
        config: KlmsConfig,
        auth_mode: str,
        course_id: str | None = None,
        since_iso: str | None = None,
        limit: int | None = None,
        bootstrap: KlmsSessionBootstrap | None = None,
        deadline: RefreshDeadline | None = None,
    ) -> ProviderLoad:
        if deadline is not None and deadline.hard_expired():
            return ProviderLoad(
                items=[],
                source="moodle_ajax",
                capability="degraded",
                freshness_mode="live",
                cache_hit=False,
                stale=False,
                fetched_at=None,
                expires_at=None,
                refresh_attempted=False,
                ok=False,
                warnings=(
                    {
                        "code": "LIVE_REFRESH_TIMEOUT",
                        "message": "Interactive refresh budget expired before the assignment refresh started.",
                    },
                ),
            )

        result = self.list_with_context(
            context=context,
            config=config,
            auth_mode=auth_mode,
            course_id=course_id,
            since_iso=since_iso,
            limit=limit,
            bootstrap=bootstrap,
        )
        rows = [row for row in result.data if isinstance(row, dict)] if isinstance(result.data, list) else []
        return ProviderLoad(
            items=rows,
            source=result.source,
            capability=result.capability,
            freshness_mode="live",
            cache_hit=False,
            stale=False,
            fetched_at=_iso_now_utc(),
            expires_at=None,
            refresh_attempted=True,
            ok=True,
        )

    def show(self, assignment_id: str, *, course_id_hint: str | None = None) -> CommandResult:
        config = load_config(self._paths)
        target_id = str(assignment_id).strip()
        if not target_id:
            raise CommandError(code="CONFIG_INVALID", message="Assignment ID is required.", exit_code=40)
        target_course_id = str(course_id_hint).strip() if course_id_hint else ""
        target_course_id = target_course_id or None

        def callback(context: Any, auth_mode: str) -> CommandResult:
            page = context.new_page()
            try:
                page.goto(abs_url(config.base_url, f"/mod/assign/view.php?id={target_id}"), wait_until="domcontentloaded", timeout=30_000)
                html = page.content()
                final_url = page.url
            finally:
                page.close()

            if looks_login_url(final_url) or looks_logged_out_html(html):
                raise CommandError(
                    code="AUTH_EXPIRED",
                    message="Saved KLMS auth did not reach the assignment page.",
                    hint="Run `kaist klms auth refresh` and try again.",
                    exit_code=10,
                    retryable=True,
                )
            if error_text := looks_klms_error_html(html):
                raise CommandError(
                    code="NOT_FOUND",
                    message=f"Assignment not found: {target_id}",
                    hint=f"KLMS returned an error page for assignment {target_id}: {error_text}",
                    exit_code=44,
                )

            assignment = _extract_assignment_detail_from_html(
                html,
                base_url=config.base_url,
                url=final_url,
                assignment_id=target_id,
                auth_mode=auth_mode,
            )
            if target_course_id and assignment.course_id and assignment.course_id != target_course_id:
                raise CommandError(
                    code="NOT_FOUND",
                    message=f"Assignment {target_id} was not found in course {target_course_id}.",
                    hint="Drop `--course-id` or pass the correct course scope.",
                    exit_code=44,
                )
            return CommandResult(data=assignment.to_dict(), source="html", capability="partial")

        return self._auth.run_authenticated(
            config=config,
            headless=True,
            accept_downloads=False,
            timeout_seconds=10.0,
            callback=callback,
        )

    def _list_api(
        self,
        *,
        context: Any,
        config: KlmsConfig,
        auth_mode: str,
        course_id: str | None,
        course_query: str | None,
        since_iso: str | None,
        limit: int | None,
        include_past: bool,
        bootstrap: KlmsSessionBootstrap | None = None,
    ) -> list[Assignment] | None:
        html = bootstrap.dashboard_html if bootstrap is not None else None
        final_url = bootstrap.dashboard_url if bootstrap is not None else None
        sesskey = bootstrap.dashboard_sesskey if bootstrap is not None else None
        if html is None or final_url is None:
            page = context.new_page()
            try:
                page.goto(config.base_url.rstrip("/") + config.dashboard_path, wait_until="domcontentloaded", timeout=30_000)
                html = page.content()
                final_url = page.url
                sesskey = extract_sesskey(html)
            finally:
                page.close()
        if looks_login_url(final_url) or looks_logged_out_html(html):
            return None
        if not sesskey:
            return None
        current_term_label = _extract_current_term_from_dashboard(html)
        current_term_course_ids = None
        if not include_past:
            discovered_current_ids = set(
                _discover_current_term_course_ids_from_dashboard(
                    html,
                    base_url=config.base_url,
                    configured_ids=config.course_ids,
                    exclude_patterns=config.exclude_course_title_patterns,
                    include_past=include_past,
                )
            )
            current_term_course_ids = discovered_current_ids or None

        methodnames = [
            "core_calendar_get_action_events_by_timesort",
            *_recommended_methodnames(self._paths, "calendar", "assignments"),
        ]
        base_limit = min(max(limit or 50, 1), 50)
        recent_floor = _epoch_from_iso(since_iso) or int(time.time()) - (180 * 24 * 3600)
        args_candidates = [
            {"limitnum": base_limit, "timesortfrom": recent_floor},
            {"limitnum": base_limit, "timesortfrom": 0},
            {},
        ]
        http = bootstrap.http if bootstrap is not None else None
        for methodname in list(dict.fromkeys(methodnames)):
            ajax_path = f"/lib/ajax/service.php?sesskey={sesskey}&info={methodname}"
            for args in args_candidates:
                payload = json.dumps([{"index": 0, "methodname": methodname, "args": args}])
                if http is not None:
                    try:
                        response_text = http.post_text(
                            ajax_path,
                            body=payload,
                            headers={
                                "Content-Type": "application/json",
                                "X-Requested-With": "XMLHttpRequest",
                                "Accept": "application/json, text/javascript, */*; q=0.01",
                            },
                            timeout_seconds=20.0,
                        ).text
                    except Exception:
                        continue
                else:
                    page = context.new_page()
                    try:
                        page.goto(final_url, wait_until="domcontentloaded", timeout=30_000)
                        result = page.evaluate(
                            """
                            async ({url, payload}) => {
                              const response = await fetch(url, {
                                method: "POST",
                                headers: {
                                  "Content-Type": "application/json",
                                  "X-Requested-With": "XMLHttpRequest",
                                  "Accept": "application/json, text/javascript, */*; q=0.01"
                                },
                                body: payload,
                                credentials: "same-origin"
                              });
                              return await response.text();
                            }
                            """,
                            {"url": config.base_url.rstrip("/") + ajax_path, "payload": payload},
                        )
                        response_text = str(result or "")
                    finally:
                        page.close()
                data = self._unwrap_moodle_ajax_data(response_text)
                if data is None:
                    continue
                assignments = _extract_assignment_rows_from_calendar_data(
                    data,
                    base_url=config.base_url,
                    auth_mode=auth_mode,
                )
                filtered = _filter_assignments(
                    assignments,
                    course_id=course_id,
                    course_query=course_query,
                    since_iso=since_iso,
                    limit=limit,
                    current_term_label=current_term_label,
                    current_term_course_ids=current_term_course_ids,
                    include_past=include_past,
                )
                if filtered:
                    return filtered
        return None

    def _list_html(
        self,
        *,
        context: Any,
        config: KlmsConfig,
        course_id: str | None,
        course_query: str | None,
        since_iso: str | None,
        limit: int | None,
        include_past: bool,
        bootstrap: KlmsSessionBootstrap | None = None,
    ) -> list[Assignment]:
        dashboard_html = bootstrap.dashboard_html if bootstrap is not None else None
        if dashboard_html is None:
            dashboard_page = context.new_page()
            try:
                dashboard_page.goto(config.base_url.rstrip("/") + config.dashboard_path, wait_until="domcontentloaded", timeout=30_000)
                dashboard_html = dashboard_page.content()
            finally:
                dashboard_page.close()

        current_term_label = _extract_current_term_from_dashboard(dashboard_html)
        current_term_course_ids = None
        if not include_past:
            discovered_current_ids = set(
                _discover_current_term_course_ids_from_dashboard(
                    dashboard_html,
                    base_url=config.base_url,
                    configured_ids=config.course_ids,
                    exclude_patterns=config.exclude_course_title_patterns,
                    include_past=include_past,
                )
            )
            current_term_course_ids = discovered_current_ids or None
        course_ids = [str(course_id).strip()] if course_id else _discover_current_term_course_ids_from_dashboard(
            dashboard_html,
            base_url=config.base_url,
            configured_ids=config.course_ids,
            exclude_patterns=config.exclude_course_title_patterns,
            include_past=include_past,
        )
        out: list[Assignment] = []
        for course in course_ids:
            if bootstrap is not None:
                html = bootstrap.http.get_html(f"/mod/assign/index.php?id={course}", context=context).text
            else:
                page = context.new_page()
                try:
                    page.goto(abs_url(config.base_url, f"/mod/assign/index.php?id={course}"), wait_until="domcontentloaded", timeout=30_000)
                    html = page.content()
                finally:
                    page.close()
            out.extend(_extract_assignments_from_index_html(html, base_url=config.base_url, course_id=course))

        return _filter_assignments(
            out,
            course_id=course_id,
            course_query=course_query,
            since_iso=since_iso,
            limit=limit,
            current_term_label=current_term_label,
            current_term_course_ids=current_term_course_ids,
            include_past=include_past,
        )

    @staticmethod
    def _unwrap_moodle_ajax_data(text: str) -> Any | None:
        try:
            payload = json.loads(text)
        except Exception:
            return None
        if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
            return None
        first = payload[0]
        if bool(first.get("error")):
            return None
        return first.get("data")


def _looks_like_attachment_url(url: str) -> bool:
    lowered = url.lower()
    if "/mod/resource/index.php" in lowered:
        return False
    return (
        "pluginfile.php" in lowered
        or "forcedownload=1" in lowered
        or "/mod/resource/view.php" in lowered
        or bool(re.search(r"\.(pdf|zip|7z|tar|gz|hwp|hwpx|doc|docx|ppt|pptx|xls|xlsx|txt|csv|py|ipynb)$", lowered))
    )


def _attachment_filename_from_url(url: str) -> str | None:
    match = re.search(r"/([^/?#]+)(?:\?|#|$)", url)
    if not match:
        return None
    return match.group(1) or None


def _collect_assignment_attachments(soup: BeautifulSoup, *, base_url: str) -> tuple[dict[str, Any], ...]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for link in soup.find_all("a", href=True):
        href = str(link.get("href") or "").strip()
        if not href or href.startswith("#") or href.lower().startswith("javascript:"):
            continue
        url = abs_url(base_url, href)
        if not _looks_like_attachment_url(url):
            continue
        if url in seen:
            continue
        seen.add(url)
        title = _norm_text(link.get_text(" ", strip=True)) or _attachment_filename_from_url(url) or url
        if title.lower() in {"course contents", "files"}:
            continue
        out.append(
            {
                "title": title,
                "url": url,
                "filename": _attachment_filename_from_url(url),
            }
        )
    return tuple(out)


def _extract_assignment_detail_from_html(
    html: str,
    *,
    base_url: str,
    url: str,
    assignment_id: str,
    auth_mode: str,
) -> Assignment:
    soup = BeautifulSoup(html, "html.parser")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()

    title = None
    course_code = None
    for selector in ("#page-header h1", ".page-header-headings h1", "h1", "title"):
        node = soup.select_one(selector)
        if not node:
            continue
        raw_title = _norm_text(node.get_text(" ", strip=True))
        title = raw_title
        if title:
            title, course_code = _split_assignment_title_context(raw_title, assignment_id=assignment_id)
            break
    if not title:
        title = f"assignment-{assignment_id}"

    course_id, course_title = _extract_course_context_from_assignment_page(soup)
    if course_title and course_title.lower() in {"course home", "course contents", "home"}:
        course_title = None

    due_raw = None
    body_text = None
    body_node = None
    for row in soup.select("table tr"):
        th = row.find("th")
        td = row.find("td")
        if not th or not td:
            continue
        key = _norm_text(th.get_text(" ", strip=True)).lower()
        value = _strip_html_text(td.get_text(" ", strip=True))
        if not value:
            continue
        if due_raw is None and any(token in key for token in ("due", "마감", "기한", "cut-off")):
            due_raw = value
        if body_text is None and any(token in key for token in ("description", "설명", "instructions", "과제")) and len(value) > 20:
            body_text = value

    if body_text is None:
        for selector in (".activity-description", ".box.py-3.generalbox", "#intro", ".no-overflow"):
            node = soup.select_one(selector)
            if not node:
                continue
            text = _norm_text(node.get_text("\n", strip=True))
            if len(text) > 20:
                body_text = text
                body_node = node
                break
    attachments = _collect_assignment_attachments(soup, base_url=base_url)

    return Assignment(
        id=assignment_id,
        title=title,
        url=url,
        due_raw=due_raw,
        due_iso=_parse_datetime_guess(due_raw) if due_raw else None,
        course_id=course_id,
        course_title=course_title,
        course_code=course_code,
        course_code_base=_course_code_base(course_code),
        body_text=body_text,
        body_html=str(body_node) if body_node is not None else None,
        attachments=attachments,
        detail_available=bool(body_text or attachments),
        source="html:assign-view",
        confidence=0.76,
        auth_mode=auth_mode,
    )
