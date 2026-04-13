from __future__ import annotations

import json
import re
from typing import Any

from bs4 import BeautifulSoup  # type: ignore[import-untyped]

from ..contracts import CommandError, CommandResult
from .auth import AuthService, extract_sesskey, looks_logged_out_html, looks_login_url
from .config import KlmsConfig, abs_url, load_config
from .discovery import load_recent_courses_args
from .models import Course
from .paths import KlmsPaths
from .session import build_session_bootstrap, fetch_html_batch
from .validate import looks_klms_error_html


SEMESTER_LABELS = {
    "1": "Spring",
    "2": "Summer",
    "3": "Fall",
    "4": "Winter",
}


def _norm_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _normalize_course_match_value(value: str | None) -> str:
    text = _norm_text(str(value or "")).lower()
    text = re.sub(r"\s+", " ", text)
    return text


def _course_title_variants(*values: str | None) -> tuple[str, ...]:
    variants: list[str] = []
    for value in values:
        text = _norm_text(str(value or ""))
        if text and text not in variants:
            variants.append(text)
    return tuple(variants)


def _course_identity_value(course: Any, key: str) -> Any:
    if isinstance(course, dict):
        return course.get(key)
    return getattr(course, key, None)


def _course_code_base(course_code: str | None) -> str | None:
    if not course_code:
        return None
    normalized = re.sub(r"_20\d{2}_\d+\s*$", "", course_code.strip())
    return normalized or None


def _extract_course_code_from_text(text: str | None) -> str | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    patterns = (
        r"\(([A-Z]{2,}[A-Z0-9_.()-]*_20\d{2}_\d+)\)",
        r"\b([A-Z]{2,}[A-Z0-9_.()-]*_20\d{2}_\d+)\b",
    )
    for pattern in patterns:
        match = re.search(pattern, raw)
        if match:
            return match.group(1).strip()
    fallback_patterns = (
        r"\b([A-Z]{2,}\.[A-Z0-9()_-]+)\b",
        r"\b([A-Z]{2,}[A-Z0-9_.()-]*_[0-9]{2,}_[0-9]+)\b",
    )
    for pattern in fallback_patterns:
        match = re.search(pattern, raw)
        if match:
            return match.group(1).strip()
    return None


def _term_label_from_course_code(course_code: str | None) -> str | None:
    text = str(course_code or "").strip()
    match = re.search(r"_((?:20)?\d{2,4})_(\d+)\s*$", text)
    if not match:
        return None
    year = match.group(1)
    semester = match.group(2)
    if len(year) == 2:
        year = f"20{year}"
    season = SEMESTER_LABELS.get(semester)
    if not season:
        return None
    return f"{year} {season}"


def _course_aliases(course: Any) -> tuple[str, ...]:
    aliases: list[str] = []

    def push(value: str | None) -> None:
        normalized = _normalize_course_match_value(value)
        if normalized and normalized not in aliases:
            aliases.append(normalized)

    course_id = str(_course_identity_value(course, "id") or _course_identity_value(course, "course_id") or "").strip() or None
    course_title = str(_course_identity_value(course, "title") or _course_identity_value(course, "course_title") or "").strip() or None
    course_code = str(_course_identity_value(course, "course_code") or "").strip() or None
    course_code_base = str(_course_identity_value(course, "course_code_base") or "").strip() or None
    title_variants = _course_identity_value(course, "title_variants")
    if not isinstance(title_variants, (list, tuple)):
        title_variants = _course_identity_value(course, "course_title_variants")
    title_candidates = _course_title_variants(course_title, *(str(value) for value in title_variants or ()))

    push(course_id)
    push(course_code)
    push(course_code_base)
    for title in title_candidates:
        push(title)

    for title in title_candidates:
        title_no_code = re.sub(r"\([^)]*_[0-9]{4}_\d+\)\s*$", "", title).strip()
        push(title_no_code)
        title_without_parens = re.sub(r"\([^)]*\)", " ", title)
        push(title_without_parens)

        acronym_tokens = re.findall(r"[A-Za-z]+", title_no_code)
        if len(acronym_tokens) >= 2:
            push("".join(token[0] for token in acronym_tokens if token))
        if acronym_tokens:
            push(" ".join(acronym_tokens))

    if course_code:
        dotted = course_code.replace("_", " ")
        push(dotted)
    if course_code_base:
        push(course_code_base.replace(".", ""))

    return tuple(aliases)


def _course_metadata_row(course: Course) -> dict[str, str | None | tuple[str, ...]]:
    return {
        "course_id": str(course.id),
        "course_title": course.title,
        "course_title_variants": _course_title_variants(course.title, *course.title_variants),
        "course_code": course.course_code,
        "course_code_base": course.course_code_base,
        "term_label": course.term_label,
    }


def _merge_course_metadata_rows(
    rows: dict[str, dict[str, str | None | tuple[str, ...]]],
    courses: list[Course],
) -> dict[str, dict[str, str | None | tuple[str, ...]]]:
    merged = {str(key): dict(value) for key, value in rows.items()}
    for course in courses:
        course_id = str(course.id).strip()
        if not course_id:
            continue
        row = merged.get(course_id) or {
            "course_id": course_id,
            "course_title": None,
            "course_title_variants": (),
            "course_code": None,
            "course_code_base": None,
            "term_label": None,
        }
        existing_title = str(row.get("course_title") or "").strip() or None
        existing_variants = row.get("course_title_variants")
        if not isinstance(existing_variants, (list, tuple)):
            existing_variants = ()
        row["course_title"] = existing_title or course.title
        row["course_title_variants"] = _course_title_variants(
            existing_title,
            *(str(value) for value in existing_variants),
            course.title,
            *course.title_variants,
        )
        row["course_code"] = str(row.get("course_code") or "").strip() or course.course_code
        row["course_code_base"] = str(row.get("course_code_base") or "").strip() or course.course_code_base
        row["term_label"] = str(row.get("term_label") or "").strip() or course.term_label
        merged[course_id] = row
    return merged


def _load_recent_courses_from_bootstrap(
    paths: KlmsPaths,
    bootstrap: Any,
    *,
    exclude_patterns: tuple[str, ...],
) -> list[Course]:
    sesskey = str(getattr(bootstrap, "dashboard_sesskey", "") or "").strip()
    http = getattr(bootstrap, "http", None)
    config = getattr(bootstrap, "config", None)
    if not sesskey or http is None or config is None or not hasattr(http, "post_text"):
        return []
    try:
        args = load_recent_courses_args(paths, limit=200)
        args["limit"] = max(200, int(args.get("limit") or 0))
        response = http.post_text(
            f"/lib/ajax/service.php?sesskey={sesskey}&info=core_course_get_recent_courses",
            body=json.dumps([{"index": 0, "methodname": "core_course_get_recent_courses", "args": args}]),
            headers={"Content-Type": "application/json"},
            timeout_seconds=10.0,
        )
        return _parse_recent_courses_payload(
            response.text,
            base_url=config.base_url,
            auth_mode=str(getattr(bootstrap, "auth_mode", "") or "") or None,
            exclude_patterns=exclude_patterns,
            include_all=True,
            include_past=True,
            limit=None,
            course_query=None,
        )
    except Exception:
        return []


def _course_matches_query(course: Course, query: str | None) -> bool:
    needle = _normalize_course_match_value(query)
    if not needle:
        return True
    aliases = _course_aliases(course)
    if any(needle == alias or needle in alias for alias in aliases):
        return True
    needle_compact = re.sub(r"[^a-z0-9가-힣]+", "", needle)
    if not needle_compact:
        return False
    for alias in aliases:
        alias_compact = re.sub(r"[^a-z0-9가-힣]+", "", alias)
        if alias_compact and needle_compact == alias_compact:
            return True
    return False


def _matching_course_aliases(course: Course, query: str | None) -> tuple[str, ...]:
    needle = _normalize_course_match_value(query)
    if not needle:
        return ()
    aliases = _course_aliases(course)
    matches: list[str] = []
    if any(needle == alias or needle in alias for alias in aliases):
        matches.extend(alias for alias in aliases if needle == alias or needle in alias)
    needle_compact = re.sub(r"[^a-z0-9가-힣]+", "", needle)
    if needle_compact:
        for alias in aliases:
            alias_compact = re.sub(r"[^a-z0-9가-힣]+", "", alias)
            if alias_compact and needle_compact == alias_compact and alias not in matches:
                matches.append(alias)
    return tuple(matches)


def _course_is_current_term(course: Course, current_term_label: str | None, *, include_past: bool) -> bool:
    if include_past or not current_term_label:
        return True
    if not course.term_label:
        return False
    return _norm_text(course.term_label).lower() == _norm_text(current_term_label).lower()


def _select_dashboard_courses(
    html: str,
    *,
    base_url: str,
    exclude_patterns: tuple[str, ...],
    course_query: str | None = None,
    include_past: bool = False,
    allow_termless_fallback: bool = False,
) -> list[Course]:
    current_term_label = _extract_current_term_from_dashboard(html)
    current_term_courses: list[Course] = []
    termless_courses: list[Course] = []
    all_courses: list[Course] = []
    for course in _discover_courses_from_dashboard(html, base_url=base_url):
        if _is_noise_course(course.title, exclude_patterns):
            continue
        if not _course_matches_query(course, course_query):
            continue
        if include_past:
            all_courses.append(course)
            continue
        if _course_is_current_term(course, current_term_label, include_past=False):
            current_term_courses.append(course)
            continue
        if allow_termless_fallback and not course.term_label:
            termless_courses.append(course)
    if include_past:
        return all_courses
    if current_term_courses:
        return current_term_courses
    if allow_termless_fallback:
        return termless_courses
    return []


def _is_noise_course(title: str, exclude_patterns: tuple[str, ...]) -> bool:
    text = (title or "").strip()
    if not text:
        return True
    default_patterns = (
        r"^Exam Bank$",
        r"^기출문제은행$",
        r"^Micro Learning$",
        r"^Teaching Skills$",
        r"^Learning Skills$",
        r"^How to use Panopto$",
        r"Panopto",
        r"Guide to KLMS",
        r"How to use KLMS",
    )
    for pattern in default_patterns + tuple(exclude_patterns):
        try:
            if re.search(pattern, text, flags=re.IGNORECASE):
                return True
        except re.error:
            continue
    return False


def _extract_current_term_from_dashboard(html: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    year_select = soup.find("select", attrs={"name": "year"})
    sem_select = soup.find("select", attrs={"name": "semester"})
    if not year_select or not sem_select:
        return None

    def selected_text(select: Any) -> str | None:
        option = select.find("option", selected=True) or select.find("option")
        if option is None:
            return None
        text = _norm_text(option.get_text(" ", strip=True))
        return text or None

    year = selected_text(year_select)
    semester = selected_text(sem_select)
    if not year or not semester:
        return None
    return f"{year} {semester}"


def _discover_courses_from_dashboard(html: str, *, base_url: str) -> list[Course]:
    soup = BeautifulSoup(html, "html.parser")
    courses: dict[str, Course] = {}
    for anchor in soup.find_all("a", href=True):
        href = str(anchor["href"])
        if "course/view.php" not in href:
            continue
        match = re.search(r"[?&]id=(\d+)", href)
        if not match:
            continue
        course_id = match.group(1)
        anchor_title = _norm_text(anchor.get_text(" ", strip=True))
        course_code = _extract_course_code_from_text(anchor_title)
        title = anchor_title or f"course-{course_id}"
        if not course_code:
            current = anchor.parent
            for _ in range(4):
                if current is None:
                    break
                block_text = _norm_text(current.get_text(" ", strip=True))
                if block_text:
                    course_code = _extract_course_code_from_text(block_text)
                    if course_code:
                        break
                current = current.parent
        courses[course_id] = Course(
            id=course_id,
            title=title,
            url=abs_url(base_url, href),
            course_code=course_code,
            course_code_base=_course_code_base(course_code),
            term_label=_term_label_from_course_code(course_code),
            title_variants=_course_title_variants(title),
            source="html:dashboard",
            confidence=0.72,
        )
    return list(courses.values())


def _extract_title_from_course_page(html: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    for selector in [
        ("div", {"class": re.compile(r"page-header-headings")}),
        ("h1", {}),
        ("title", {}),
    ]:
        element = soup.find(selector[0], selector[1])
        if element is None:
            continue
        text = _norm_text(element.get_text(" ", strip=True))
        if text:
            return re.sub(r"^\s*Course:\s*", "", text, flags=re.IGNORECASE).strip()
    return None


def _extract_course_code_from_resource_index(html: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    title = _norm_text(soup.title.get_text(" ", strip=True)) if soup.title else None
    if not title:
        return None
    match = re.match(r"^([^:]+)\s*:\s*Files\s*$", title)
    if match:
        code = _norm_text(match.group(1))
        if code and code.lower() not in {"files", "dashboard"}:
            return code
    match = re.search(r"\b[A-Z]{2,}\d{2,}[A-Z0-9_()]*\b", title)
    return match.group(0) if match else None


def _split_person_names(raw: str) -> list[str]:
    text = _norm_text(raw)
    if not text:
        return []
    text = re.sub(r"^(professors?|instructors?|teachers?|담당교수|교수진)\s*[:：]?\s*", "", text, flags=re.IGNORECASE)
    chunks = [chunk.strip() for chunk in re.split(r"[,/;|·\n]+", text) if chunk.strip()]
    return list(dict.fromkeys(chunks))


def _extract_professors_from_course_page(html: str) -> tuple[str, ...]:
    soup = BeautifulSoup(html, "html.parser")
    names: list[str] = []
    role_hint = re.compile(r"^(professors?|instructors?|teachers?|담당교수|교수진|교강사)\s*$", re.IGNORECASE)
    role_in_key = re.compile(r"(professor|instructor|teacher|담당교수|교수진|교강사)", re.IGNORECASE)

    labels = [node for node in soup.find_all(string=True) if role_hint.match(_norm_text(str(node)))]
    for label in labels:
        container = label.parent
        block = container.parent if container is not None and getattr(container.parent, "name", None) else container
        if block is None:
            continue
        anchors = list(block.find_all("a"))
        if anchors:
            for anchor in anchors:
                names.extend(_split_person_names(anchor.get_text(" ", strip=True)))
        else:
            names.extend(_split_person_names(block.get_text(" ", strip=True)))

    if not names:
        for row in soup.find_all("tr"):
            header = row.find("th")
            value = row.find("td")
            if header is None or value is None:
                continue
            key = _norm_text(header.get_text(" ", strip=True))
            if role_in_key.search(key):
                names.extend(_split_person_names(value.get_text(" ", strip=True)))

    return tuple(dict.fromkeys(name for name in names if name))


def _parse_recent_courses_payload(
    payload_text: str,
    *,
    base_url: str,
    auth_mode: str,
    exclude_patterns: tuple[str, ...],
    include_all: bool,
    include_past: bool = False,
    limit: int | None,
    current_term_label: str | None = None,
    term_label: str | None = None,
    course_query: str | None = None,
) -> list[Course]:
    if current_term_label is None:
        current_term_label = term_label
    try:
        payload = json.loads(payload_text)
    except Exception as exc:  # noqa: BLE001
        raise CommandError(
            code="API_SHAPE_CHANGED",
            message=f"Failed to parse core_course_get_recent_courses JSON: {exc}",
            hint="Run `kaist klms dev probe --live` to inspect the current payload shape.",
            exit_code=30,
            retryable=True,
        ) from exc

    if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
        raise CommandError(
            code="API_SHAPE_CHANGED",
            message="Unexpected core_course_get_recent_courses response shape.",
            hint="Run `kaist klms dev probe --live` to inspect the current payload shape.",
            exit_code=30,
            retryable=True,
        )
    first = payload[0]
    if bool(first.get("error")):
        raise CommandError(
            code="API_SHAPE_CHANGED",
            message=f"core_course_get_recent_courses returned an error payload: {first}",
            hint="Refresh auth or rerun the live probe to confirm the endpoint is still valid.",
            exit_code=30,
            retryable=True,
        )
    rows = first.get("data") or []
    if not isinstance(rows, list):
        raise CommandError(
            code="API_SHAPE_CHANGED",
            message="core_course_get_recent_courses did not return a course list.",
            hint="Run `kaist klms dev probe --live` to inspect the payload.",
            exit_code=30,
            retryable=True,
        )

    courses: list[Course] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        course_id = str(row.get("id") or "").strip()
        if not course_id:
            continue
        fullname = _norm_text(str(row.get("fullname") or "")) or None
        fullnamedisplay = _norm_text(str(row.get("fullnamedisplay") or "")) or None
        title_variants = _course_title_variants(fullname, fullnamedisplay)
        title = next(iter(title_variants), f"course-{course_id}")
        if not include_all and _is_noise_course(title, exclude_patterns):
            continue
        course_code = str(row.get("shortname") or "").strip() or None
        course = Course(
            id=course_id,
            title=title,
            url=str(view_url).strip() if isinstance((view_url := row.get("viewurl")), str) and str(view_url).strip() else abs_url(base_url, f"/course/view.php?id={course_id}"),
            course_code=course_code,
            course_code_base=_course_code_base(course_code),
            term_label=_term_label_from_course_code(course_code),
            title_variants=title_variants,
            source="ajax:core_course_get_recent_courses",
            confidence=0.9,
            auth_mode=auth_mode,
        )
        if not _course_is_current_term(course, current_term_label, include_past=include_past):
            continue
        if not _course_matches_query(course, course_query):
            continue
        courses.append(course)
    if limit is not None:
        courses = courses[: max(0, limit)]
    return courses


def _course_page_path(course_id: str) -> str:
    return f"/course/view.php?id={course_id}&section=0"


class CourseService:
    def __init__(self, paths: KlmsPaths, auth: AuthService) -> None:
        self._paths = paths
        self._auth = auth

    def _list_courses_ajax(
        self,
        *,
        context: Any,
        config: KlmsConfig,
        auth_mode: str,
        include_all: bool,
        include_past: bool,
        limit: int | None,
        course_query: str | None,
    ) -> list[Course] | None:
        page = context.new_page()
        try:
            page.goto(config.base_url.rstrip("/") + config.dashboard_path, wait_until="domcontentloaded", timeout=30_000)
            html = page.content()
            final_url = page.url
            if looks_login_url(final_url) or looks_logged_out_html(html):
                return None
            sesskey = extract_sesskey(html)
            if not sesskey:
                return None
            current_term_label = _extract_current_term_from_dashboard(html)
            args = load_recent_courses_args(self._paths, limit=limit)
            payload = [{"index": 0, "methodname": "core_course_get_recent_courses", "args": args}]
            ajax_url = f"{config.base_url.rstrip('/')}/lib/ajax/service.php?sesskey={sesskey}&info=core_course_get_recent_courses"
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
                    body: JSON.stringify(payload),
                    credentials: "same-origin"
                  });
                  const text = await response.text();
                  return {
                    ok: response.ok,
                    status: response.status,
                    url: response.url,
                    text
                  };
                }
                """,
                {"url": ajax_url, "payload": payload},
            )
        finally:
            page.close()

        if not isinstance(result, dict):
            return None
        if not bool(result.get("ok")):
            return None
        return _parse_recent_courses_payload(
            str(result.get("text") or ""),
            base_url=config.base_url,
            auth_mode=auth_mode,
            exclude_patterns=config.exclude_course_title_patterns,
            include_all=include_all,
            include_past=include_past,
            limit=limit,
            current_term_label=current_term_label,
            course_query=course_query,
        )

    def _enrich_course_professors(
        self,
        *,
        context: Any,
        config: KlmsConfig,
        auth_mode: str,
        courses: list[Course],
    ) -> list[Course]:
        targets = [course for course in courses if not course.professors][:12]
        if not targets:
            return courses
        bootstrap = build_session_bootstrap(
            self._paths,
            context=context,
            config=config,
            auth_mode=auth_mode,
        )
        response_map = fetch_html_batch(
            bootstrap.http,
            [_course_page_path(course.id) for course in targets],
            max_workers=min(4, len(targets)),
        )
        professors_by_id: dict[str, tuple[str, ...]] = {}
        for course in targets:
            response = response_map.get(_course_page_path(course.id))
            if response is None:
                continue
            if looks_login_url(response.url) or looks_logged_out_html(response.text):
                continue
            professors = _extract_professors_from_course_page(response.text)
            if professors:
                professors_by_id[course.id] = professors
        return [
            Course(
                id=course.id,
                title=course.title,
                url=course.url,
                course_code=course.course_code,
                course_code_base=course.course_code_base,
                term_label=course.term_label,
                title_variants=course.title_variants,
                professors=professors_by_id.get(course.id, course.professors),
                source=course.source,
                confidence=course.confidence,
                auth_mode=course.auth_mode,
            )
            for course in courses
        ]

    def _list_courses_html(
        self,
        *,
        context: Any,
        config: KlmsConfig,
        auth_mode: str,
        include_all: bool,
        include_past: bool,
        limit: int | None,
        course_query: str | None,
    ) -> list[Course]:
        page = context.new_page()
        try:
            page.goto(config.base_url.rstrip("/") + config.dashboard_path, wait_until="domcontentloaded", timeout=30_000)
            html = page.content()
        finally:
            page.close()

        current_term_label = _extract_current_term_from_dashboard(html)
        courses = _discover_courses_from_dashboard(html, base_url=config.base_url)
        out: list[Course] = []
        for course in courses:
            if not include_all and _is_noise_course(course.title, config.exclude_course_title_patterns):
                continue
            resolved = Course(
                id=course.id,
                title=course.title,
                url=course.url,
                course_code=course.course_code,
                course_code_base=course.course_code_base,
                term_label=course.term_label,
                title_variants=course.title_variants,
                source="html:dashboard",
                confidence=0.72,
                auth_mode=auth_mode,
            )
            if not _course_is_current_term(resolved, current_term_label, include_past=include_past):
                continue
            if not _course_matches_query(resolved, course_query):
                continue
            out.append(resolved)
        if limit is not None:
            out = out[: max(0, limit)]
        return out

    def list(
        self,
        *,
        include_all: bool = False,
        include_past: bool = False,
        limit: int | None = None,
        course_query: str | None = None,
    ) -> CommandResult:
        config = load_config(self._paths)

        def callback(context: Any, auth_mode: str) -> CommandResult:
            ajax_courses = self._list_courses_ajax(
                context=context,
                config=config,
                auth_mode=auth_mode,
                include_all=include_all,
                include_past=include_past,
                limit=limit,
                course_query=course_query,
            )
            if ajax_courses:
                ajax_courses = self._enrich_course_professors(
                    context=context,
                    config=config,
                    auth_mode=auth_mode,
                    courses=ajax_courses,
                )
                return CommandResult(
                    data=[course.to_dict() for course in ajax_courses],
                    source="moodle_ajax",
                    capability="full",
                )

            html_courses = self._list_courses_html(
                context=context,
                config=config,
                auth_mode=auth_mode,
                include_all=include_all,
                include_past=include_past,
                limit=limit,
                course_query=course_query,
            )
            html_courses = self._enrich_course_professors(
                context=context,
                config=config,
                auth_mode=auth_mode,
                courses=html_courses,
            )
            return CommandResult(
                data=[course.to_dict() for course in html_courses],
                source="html",
                capability="partial",
            )

        return self._auth.run_authenticated(
            config=config,
            headless=True,
            accept_downloads=False,
            timeout_seconds=10.0,
            callback=callback,
        )

    def show(self, course_id: str) -> CommandResult:
        config = load_config(self._paths)
        target_id = str(course_id).strip()
        if not target_id:
            raise CommandError(code="CONFIG_INVALID", message="Course ID is required.", exit_code=40)

        def callback(context: Any, auth_mode: str) -> CommandResult:
            page = context.new_page()
            try:
                page.goto(abs_url(config.base_url, f"/course/view.php?id={target_id}&section=0"), wait_until="domcontentloaded", timeout=30_000)
                course_html = page.content()
                course_url = page.url
            finally:
                page.close()

            page = context.new_page()
            try:
                page.goto(abs_url(config.base_url, f"/mod/resource/index.php?id={target_id}"), wait_until="domcontentloaded", timeout=30_000)
                files_html = page.content()
            finally:
                page.close()

            if error_text := looks_klms_error_html(course_html):
                raise CommandError(
                    code="NOT_FOUND",
                    message=f"Course not found: {target_id}",
                    hint=f"KLMS returned an error page for course {target_id}: {error_text}",
                    exit_code=44,
                )

            title = _extract_title_from_course_page(course_html) or f"course-{target_id}"
            course_code = _extract_course_code_from_resource_index(files_html)
            professors = _extract_professors_from_course_page(course_html)
            course = Course(
                id=target_id,
                title=title,
                url=course_url,
                course_code=course_code,
                course_code_base=_course_code_base(course_code),
                term_label=_term_label_from_course_code(course_code),
                title_variants=_course_title_variants(title),
                professors=professors,
                source="html:course-page",
                confidence=0.78,
                auth_mode=auth_mode,
            )
            return CommandResult(data=course.to_dict(), source="html", capability="partial")

        return self._auth.run_authenticated(
            config=config,
            headless=True,
            accept_downloads=False,
            timeout_seconds=10.0,
            callback=callback,
        )

    def resolve(
        self,
        *,
        query: str,
        include_all: bool = False,
        include_past: bool = True,
        limit: int | None = 10,
    ) -> CommandResult:
        needle = str(query or "").strip()
        if not needle:
            raise CommandError(
                code="CONFIG_INVALID",
                message="Course resolve requires a non-empty query.",
                hint="Pass a course ID, code, Korean title, or English title.",
                exit_code=40,
                retryable=False,
            )
        config = load_config(self._paths)
        limit = None if limit is None else max(1, min(int(limit), 50))

        def callback(context: Any, auth_mode: str) -> CommandResult:
            courses = self._list_courses_ajax(
                context=context,
                config=config,
                auth_mode=auth_mode,
                include_all=include_all,
                include_past=include_past,
                limit=None,
                course_query=None,
            )
            source = "moodle_ajax"
            capability = "full"
            if not courses:
                courses = self._list_courses_html(
                    context=context,
                    config=config,
                    auth_mode=auth_mode,
                    include_all=include_all,
                    include_past=include_past,
                    limit=None,
                    course_query=None,
                )
                source = "html"
                capability = "partial"
            courses = self._enrich_course_professors(
                context=context,
                config=config,
                auth_mode=auth_mode,
                courses=courses,
            )
            matches: list[dict[str, Any]] = []
            for course in courses:
                matched_aliases = _matching_course_aliases(course, needle)
                if not matched_aliases:
                    continue
                row = course.to_dict()
                row["matched_aliases"] = list(matched_aliases)
                row["title_variants"] = list(course.title_variants)
                matches.append(row)
            matches.sort(
                key=lambda row: (
                    0 if needle.lower() in {alias.lower() for alias in row["matched_aliases"]} else 1,
                    len(row["matched_aliases"]),
                    str(row.get("title") or "").lower(),
                )
            )
            if limit is not None:
                matches = matches[:limit]
            resolution = "none"
            if len(matches) == 1:
                resolution = "unique"
            elif matches:
                resolution = "ambiguous"
            return CommandResult(
                data={
                    "query": needle,
                    "resolution": resolution,
                    "count": len(matches),
                    "items": matches,
                },
                source=source,
                capability=capability,
            )

        return self._auth.run_authenticated(
            config=config,
            headless=True,
            accept_downloads=False,
            timeout_seconds=10.0,
            callback=callback,
        )
