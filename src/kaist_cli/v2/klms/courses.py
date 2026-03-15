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


def _norm_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _course_code_base(course_code: str | None) -> str | None:
    if not course_code:
        return None
    normalized = re.sub(r"_20\d{2}_\d+\s*$", "", course_code.strip())
    return normalized or None


def _is_noise_course(title: str, exclude_patterns: tuple[str, ...]) -> bool:
    text = (title or "").strip()
    if not text:
        return True
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
        title = _norm_text(anchor.get_text(" ", strip=True)) or f"course-{course_id}"
        courses[course_id] = Course(
            id=course_id,
            title=title,
            url=abs_url(base_url, href),
            course_code=None,
            course_code_base=None,
            term_label=None,
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
    limit: int | None,
    term_label: str | None = None,
) -> list[Course]:
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
        title = _norm_text(str(row.get("fullname") or row.get("fullnamedisplay") or f"course-{course_id}"))
        if not include_all and _is_noise_course(title, exclude_patterns):
            continue
        course_code = str(row.get("shortname") or "").strip() or None
        view_url = row.get("viewurl")
        courses.append(
            Course(
                id=course_id,
                title=title,
                url=str(view_url).strip() if isinstance(view_url, str) and view_url.strip() else abs_url(base_url, f"/course/view.php?id={course_id}"),
                course_code=course_code,
                course_code_base=_course_code_base(course_code),
                term_label=term_label,
                source="ajax:core_course_get_recent_courses",
                confidence=0.9,
                auth_mode=auth_mode,
            )
        )
    if limit is not None:
        courses = courses[: max(0, limit)]
    return courses


class CourseService:
    def __init__(self, paths: KlmsPaths, auth: AuthService) -> None:
        self._paths = paths
        self._auth = auth

    def _list_courses_ajax(self, *, context: Any, config: KlmsConfig, auth_mode: str, include_all: bool, limit: int | None) -> list[Course] | None:
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
            term_label = _extract_current_term_from_dashboard(html)
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
            limit=limit,
            term_label=term_label,
        )

    def _list_courses_html(self, *, context: Any, config: KlmsConfig, auth_mode: str, include_all: bool, limit: int | None) -> list[Course]:
        page = context.new_page()
        try:
            page.goto(config.base_url.rstrip("/") + config.dashboard_path, wait_until="domcontentloaded", timeout=30_000)
            html = page.content()
        finally:
            page.close()

        term_label = _extract_current_term_from_dashboard(html)
        courses = _discover_courses_from_dashboard(html, base_url=config.base_url)
        out: list[Course] = []
        for course in courses:
            if not include_all and _is_noise_course(course.title, config.exclude_course_title_patterns):
                continue
            out.append(
                Course(
                    id=course.id,
                    title=course.title,
                    url=course.url,
                    course_code=course.course_code,
                    course_code_base=course.course_code_base,
                    term_label=term_label,
                    source="html:dashboard",
                    confidence=0.72,
                    auth_mode=auth_mode,
                )
            )
        if limit is not None:
            out = out[: max(0, limit)]
        return out

    def list(self, *, include_all: bool = False, limit: int | None = None) -> CommandResult:
        config = load_config(self._paths)

        def callback(context: Any, auth_mode: str) -> CommandResult:
            ajax_courses = self._list_courses_ajax(
                context=context,
                config=config,
                auth_mode=auth_mode,
                include_all=include_all,
                limit=limit,
            )
            if ajax_courses:
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
                limit=limit,
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
            finally:
                page.close()

            page = context.new_page()
            try:
                page.goto(abs_url(config.base_url, f"/mod/resource/index.php?id={target_id}"), wait_until="domcontentloaded", timeout=30_000)
                files_html = page.content()
            finally:
                page.close()

            title = _extract_title_from_course_page(course_html) or f"course-{target_id}"
            course_code = _extract_course_code_from_resource_index(files_html)
            professors = _extract_professors_from_course_page(course_html)
            course = Course(
                id=target_id,
                title=title,
                url=abs_url(config.base_url, f"/course/view.php?id={target_id}"),
                course_code=course_code,
                course_code_base=_course_code_base(course_code),
                term_label=None,
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
