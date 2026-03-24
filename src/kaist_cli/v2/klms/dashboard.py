from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Any, Callable

from ..contracts import CommandError, CommandResult
from .auth import AuthService
from .assignments import AssignmentService
from .config import load_config
from .deadline import RefreshDeadline
from .files import FileService
from .notices import NoticeService
from .paths import KlmsPaths
from .provider_state import ProviderLoad
from .session import build_session_bootstrap


def _local_now() -> datetime:
    return datetime.now().astimezone()


def _parse_iso_datetime(value: str | None, *, local_tz: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    normalized = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=local_tz)
    return dt.astimezone(local_tz)


def _kind_priority(kind: str) -> int:
    return {"assignment": 0, "notice": 1, "file": 2}.get(kind, 9)


def _ranked_timestamp(item: dict[str, Any], *, local_tz: Any) -> float:
    dt = _parse_iso_datetime(str(item.get("time_iso") or ""), local_tz=local_tz)
    return dt.timestamp() if dt is not None else float("-inf")


def _build_inbox_items(
    *,
    assignments: list[dict[str, Any]],
    notices: list[dict[str, Any]],
    files: list[dict[str, Any]],
    limit: int,
) -> list[dict[str, Any]]:
    local_tz = _local_now().tzinfo
    items: list[dict[str, Any]] = []

    for row in assignments:
        items.append(
            {
                "kind": "assignment",
                "id": row.get("id"),
                "title": row.get("title"),
                "url": row.get("url"),
                "course_id": row.get("course_id"),
                "course_title": row.get("course_title"),
                "time_iso": row.get("due_iso"),
                "due_iso": row.get("due_iso"),
                "source": row.get("source"),
                "confidence": row.get("confidence"),
            }
        )

    for row in notices:
        items.append(
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
        items.append(
            {
                "kind": "file",
                "id": row.get("id") or row.get("url"),
                "title": row.get("title"),
                "url": row.get("url"),
                "download_url": row.get("download_url"),
                "course_id": row.get("course_id"),
                "course_title": row.get("course_title"),
                "time_iso": row.get("first_seen_at"),
                "first_seen_at": row.get("first_seen_at"),
                "last_seen_at": row.get("last_seen_at"),
                "source": row.get("source"),
                "confidence": row.get("confidence"),
                "downloadable": row.get("downloadable"),
                "file_kind": row.get("kind"),
            }
        )

    items.sort(
        key=lambda item: (
            -_ranked_timestamp(item, local_tz=local_tz),
            _kind_priority(str(item.get("kind") or "")),
            str(item.get("title") or "").lower(),
        )
    )
    return items[: max(0, limit)]


def _filter_inbox_assignments(
    assignments: list[dict[str, Any]],
    *,
    now: datetime,
    future_days: int = 30,
    past_days: int = 14,
) -> list[dict[str, Any]]:
    floor = now - timedelta(days=past_days)
    ceiling = now + timedelta(days=future_days)
    out: list[dict[str, Any]] = []
    for row in assignments:
        due_dt = _parse_iso_datetime(str(row.get("due_iso") or ""), local_tz=now.tzinfo)
        if due_dt is None:
            continue
        if floor <= due_dt <= ceiling:
            out.append(row)
    return out


def _filter_inbox_files(
    files: list[dict[str, Any]],
    *,
    since_iso: str | None,
    now: datetime,
) -> list[dict[str, Any]]:
    floor = _parse_iso_datetime(since_iso, local_tz=now.tzinfo) if since_iso else None
    out: list[dict[str, Any]] = []
    for row in files:
        if not bool(row.get("downloadable")):
            continue
        if floor is None:
            out.append(row)
            continue
        seen_dt = _parse_iso_datetime(str(row.get("first_seen_at") or row.get("last_seen_at") or ""), local_tz=now.tzinfo)
        if seen_dt is not None and seen_dt >= floor:
            out.append(row)
    return out


def _decorate_today_assignments(
    assignments: list[dict[str, Any]],
    *,
    now: datetime,
    window_days: int,
    overdue_grace_days: int = 2,
    limit: int,
) -> list[dict[str, Any]]:
    end = now + timedelta(days=window_days)
    overdue_floor = now - timedelta(days=overdue_grace_days)
    out: list[dict[str, Any]] = []
    for row in assignments:
        due_dt = _parse_iso_datetime(str(row.get("due_iso") or ""), local_tz=now.tzinfo)
        if due_dt is None:
            continue
        if due_dt > end:
            continue
        if due_dt < overdue_floor:
            continue
        delta = due_dt - now
        if due_dt < now:
            status = "overdue"
        elif due_dt.date() == now.date():
            status = "due_today"
        else:
            status = "due_soon"
        decorated = dict(row)
        decorated["status"] = status
        decorated["hours_until_due"] = round(delta.total_seconds() / 3600, 2)
        decorated["days_until_due"] = round(delta.total_seconds() / 86400, 2)
        out.append(decorated)

    status_rank = {"overdue": 0, "due_today": 1, "due_soon": 2}
    out.sort(
        key=lambda row: (
            status_rank.get(str(row.get("status") or ""), 9),
            _parse_iso_datetime(str(row.get("due_iso") or ""), local_tz=now.tzinfo) or datetime.max.replace(tzinfo=now.tzinfo),
            str(row.get("title") or "").lower(),
        )
    )
    return out[: max(0, limit)]


def _select_recent_notices(
    notices: list[dict[str, Any]],
    *,
    now: datetime,
    notice_days: int,
    limit: int,
) -> list[dict[str, Any]]:
    floor = now - timedelta(days=notice_days)
    out: list[dict[str, Any]] = []
    for row in notices:
        posted_dt = _parse_iso_datetime(str(row.get("posted_iso") or ""), local_tz=now.tzinfo)
        if posted_dt is None or posted_dt < floor:
            continue
        decorated = dict(row)
        decorated["hours_since_posted"] = round((now - posted_dt).total_seconds() / 3600, 2)
        out.append(decorated)
    out.sort(
        key=lambda row: (
            -(_parse_iso_datetime(str(row.get("posted_iso") or ""), local_tz=now.tzinfo) or floor).timestamp(),
            str(row.get("title") or "").lower(),
        )
    )
    return out[: max(0, limit)]


def _select_materials(files: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    materials = [dict(row) for row in files if bool(row.get("downloadable"))]
    local_tz = _local_now().tzinfo
    for row in materials:
        seen_dt = _parse_iso_datetime(str(row.get("first_seen_at") or row.get("last_seen_at") or ""), local_tz=local_tz)
        if seen_dt is not None:
            row["hours_since_seen"] = round((_local_now() - seen_dt).total_seconds() / 3600, 2)
    materials.sort(
        key=lambda row: (
            -_ranked_timestamp({"time_iso": row.get("first_seen_at") or row.get("last_seen_at")}, local_tz=local_tz),
            str(row.get("course_title") or ""),
            str(row.get("title") or "").lower(),
        )
    )
    return materials[: max(0, limit)]


def _merge_capability(results: list[ProviderLoad], *, had_warnings: bool) -> str:
    values = [result.capability for result in results if result.ok]
    if had_warnings:
        return "degraded"
    if any(not result.ok for result in results):
        return "degraded"
    if values and all(value == "full" for value in values):
        return "full"
    if "partial" in values:
        return "partial"
    if "degraded" in values:
        return "degraded"
    return values[0] if values else "partial"


class DashboardService:
    def __init__(
        self,
        paths: KlmsPaths,
        auth: AuthService,
        assignments: AssignmentService,
        notices: NoticeService,
        files: FileService,
    ) -> None:
        self._paths = paths
        self._auth = auth
        self._assignments = assignments
        self._notices = notices
        self._files = files

    def inbox(
        self,
        *,
        limit: int = 30,
        max_notice_pages: int = 1,
        since_iso: str | None = None,
    ) -> CommandResult:
        limit = max(1, min(int(limit), 500))
        now = _local_now()
        config = load_config(self._paths)
        deadline = RefreshDeadline.start()
        notice_floor_iso = since_iso or (now - timedelta(days=21)).isoformat(timespec="seconds")

        def callback(context: Any, auth_mode: str, dashboard_state: dict[str, Any]) -> CommandResult:
            bootstrap = build_session_bootstrap(
                self._paths,
                context=context,
                config=config,
                auth_mode=auth_mode,
                timeout_seconds=10.0,
                dashboard_url=str(dashboard_state.get("final_url") or ""),
                dashboard_html=str(dashboard_state.get("html") or ""),
            )
            warnings: list[dict[str, Any]] = []
            provider_status: dict[str, Any] = {}
            loads = self._run_components_parallel(
                [
                    (
                        "assignments",
                        lambda: self._assignments.load_for_dashboard(
                            context=context,
                            config=config,
                            auth_mode=auth_mode,
                            limit=max(limit * 2, 12),
                            since_iso=since_iso or (now - timedelta(days=14)).isoformat(timespec="seconds"),
                            bootstrap=bootstrap,
                            deadline=deadline,
                        ),
                    ),
                    (
                        "notices",
                        lambda: self._notices.load_for_dashboard(
                            context=context,
                            config=config,
                            auth_mode=auth_mode,
                            max_pages=max_notice_pages,
                            limit=max(limit * 2, 12),
                            since_iso=notice_floor_iso,
                            bootstrap=bootstrap,
                            deadline=deadline,
                        ),
                    ),
                    (
                        "files",
                        lambda: self._files.load_for_dashboard(
                            context=context,
                            config=config,
                            auth_mode=auth_mode,
                            limit=max(limit, 6),
                            bootstrap=bootstrap,
                            deadline=deadline,
                        ),
                    ),
                ],
                warnings=warnings,
                provider_status=provider_status,
            )
            successes: list[ProviderLoad] = []
            assignments_load = loads["assignments"]
            if assignments_load.ok:
                successes.append(assignments_load)
            assignments_data = _filter_inbox_assignments(assignments_load.items, now=now)

            notices_load = loads["notices"]
            if notices_load.ok:
                successes.append(notices_load)

            files_load = loads["files"]
            if files_load.ok:
                successes.append(files_load)
            files_data = _filter_inbox_files(files_load.items, since_iso=since_iso, now=now)

            items = _build_inbox_items(
                assignments=assignments_data,
                notices=notices_load.items,
                files=files_data,
                limit=limit,
            )
            payload = {
                "items": items,
                "providers": provider_status,
                "warnings": warnings,
            }
            return CommandResult(
                data=payload,
                source="mixed",
                capability=_merge_capability(successes, had_warnings=bool(warnings)),
            )

        return self._auth.run_authenticated_with_state(
            config=config,
            headless=True,
            accept_downloads=False,
            timeout_seconds=deadline.request_timeout(10.0, use_soft=False),
            callback=callback,
        )

    def today(
        self,
        *,
        limit: int = 5,
        window_days: int = 7,
        notice_days: int = 3,
        max_notice_pages: int = 1,
    ) -> CommandResult:
        limit = max(1, min(int(limit), 50))
        window_days = max(1, min(int(window_days), 30))
        notice_days = max(1, min(int(notice_days), 14))
        now = _local_now()
        config = load_config(self._paths)
        deadline = RefreshDeadline.start()
        notice_floor_iso = (now - timedelta(days=notice_days)).isoformat(timespec="seconds")

        def callback(context: Any, auth_mode: str, dashboard_state: dict[str, Any]) -> CommandResult:
            bootstrap = build_session_bootstrap(
                self._paths,
                context=context,
                config=config,
                auth_mode=auth_mode,
                timeout_seconds=10.0,
                dashboard_url=str(dashboard_state.get("final_url") or ""),
                dashboard_html=str(dashboard_state.get("html") or ""),
            )
            warnings: list[dict[str, Any]] = []
            provider_status: dict[str, Any] = {}
            loads = self._run_components_parallel(
                [
                    (
                        "assignments",
                        lambda: self._assignments.load_for_dashboard(
                            context=context,
                            config=config,
                            auth_mode=auth_mode,
                            limit=max(limit * 3, 12),
                            since_iso=(now - timedelta(days=2)).isoformat(timespec="seconds"),
                            bootstrap=bootstrap,
                            deadline=deadline,
                        ),
                    ),
                    (
                        "notices",
                        lambda: self._notices.load_for_dashboard(
                            context=context,
                            config=config,
                            auth_mode=auth_mode,
                            max_pages=max_notice_pages,
                            limit=max(limit * 2, 12),
                            since_iso=notice_floor_iso,
                            bootstrap=bootstrap,
                            deadline=deadline,
                        ),
                    ),
                    (
                        "files",
                        lambda: self._files.load_for_dashboard(
                            context=context,
                            config=config,
                            auth_mode=auth_mode,
                            limit=max(limit, 6),
                            bootstrap=bootstrap,
                            deadline=deadline,
                        ),
                    ),
                ],
                warnings=warnings,
                provider_status=provider_status,
            )
            successes: list[ProviderLoad] = []
            assignments_load = loads["assignments"]
            if assignments_load.ok:
                successes.append(assignments_load)

            notices_load = loads["notices"]
            if notices_load.ok:
                successes.append(notices_load)

            files_load = loads["files"]
            if files_load.ok:
                successes.append(files_load)

            urgent_assignments = _decorate_today_assignments(assignments_load.items, now=now, window_days=window_days, limit=limit)
            recent_notices = _select_recent_notices(notices_load.items, now=now, notice_days=notice_days, limit=limit)
            materials = _select_materials(files_load.items, limit=limit)

            payload = {
                "summary": {
                    "now_iso": now.isoformat(timespec="seconds"),
                    "window_days": window_days,
                    "notice_days": notice_days,
                    "urgent_assignment_count": len(urgent_assignments),
                    "recent_notice_count": len(recent_notices),
                    "material_count": len(materials),
                },
                "providers": provider_status,
                "warnings": warnings,
                "urgent_assignments": urgent_assignments,
                "recent_notices": recent_notices,
                "materials": materials,
            }
            return CommandResult(
                data=payload,
                source="mixed",
                capability=_merge_capability(successes, had_warnings=bool(warnings)),
            )

        return self._auth.run_authenticated_with_state(
            config=config,
            headless=True,
            accept_downloads=False,
            timeout_seconds=deadline.request_timeout(10.0, use_soft=False),
            callback=callback,
        )

    @classmethod
    def _run_component(
        cls,
        name: str,
        runner: Callable[[], ProviderLoad],
    ) -> ProviderLoad:
        try:
            result = runner()
        except TimeoutError:
            result = ProviderLoad(
                items=[],
                source="mixed",
                capability="degraded",
                freshness_mode="live",
                cache_hit=False,
                stale=False,
                fetched_at=None,
                expires_at=None,
                refresh_attempted=True,
                ok=False,
                warnings=(
                    {
                        "code": "LIVE_REFRESH_TIMEOUT",
                        "message": f"{name} refresh exceeded the interactive deadline.",
                    },
                ),
            )
        except CommandError as error:
            if error.code in {"AUTH_MISSING", "AUTH_EXPIRED"}:
                raise
            result = ProviderLoad(
                items=[],
                source="mixed",
                capability="degraded",
                freshness_mode="live",
                cache_hit=False,
                stale=False,
                fetched_at=None,
                expires_at=None,
                refresh_attempted=True,
                ok=False,
                warnings=(
                    {
                        "code": "LIVE_REFRESH_FAILED",
                        "message": error.message,
                        "error_code": error.code,
                    },
                ),
            )
        return result

    @classmethod
    def _run_components_parallel(
        cls,
        components: list[tuple[str, Callable[[], ProviderLoad]]],
        *,
        warnings: list[dict[str, Any]],
        provider_status: dict[str, Any],
    ) -> dict[str, ProviderLoad]:
        results: dict[str, ProviderLoad] = {}
        with ThreadPoolExecutor(max_workers=max(1, min(3, len(components)))) as executor:
            futures = {executor.submit(cls._run_component, name, runner): name for name, runner in components}
            for future, name in futures.items():
                result = future.result()
                results[name] = result
        for name, _runner in components:
            result = results[name]
            warnings.extend(result.provider_warnings(name))
            provider_status[name] = result.provider_status()
        return results
