from __future__ import annotations

import argparse
import asyncio
import json
import sys
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True)
class CliErrorDescriptor:
    code: str
    exit_code: int
    retryable: bool
    hint: str | None


def _utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _run_async(coro: Any) -> Any:
    return asyncio.run(coro)


def _run_klms_async(coro: Any) -> Any:
    from . import klms

    async def wrapped() -> Any:
        async with klms.klms_runtime(headless=True, accept_downloads=True):
            return await coro

    return _run_async(wrapped())


def _is_tabular_list(data: Any) -> bool:
    return isinstance(data, list) and all(isinstance(item, dict) for item in data)


def _table_columns(rows: list[dict[str, Any]]) -> list[str]:
    priority = [
        "id",
        "board_id",
        "course_id",
        "title",
        "due_iso",
        "posted_iso",
        "course_code_base",
        "course_code",
        "term_label",
        "kind",
        "url",
        "path",
        "source",
    ]
    keys = {k for row in rows for k in row.keys()}
    ordered = [k for k in priority if k in keys]
    extra = sorted(k for k in keys if k not in ordered)
    return ordered + extra


def _format_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _emit_table(rows: list[dict[str, Any]]) -> None:
    if not rows:
        print("(no rows)")
        return
    columns = _table_columns(rows)
    widths: dict[str, int] = {}
    for col in columns:
        max_cell = max(len(_format_cell(row.get(col))) for row in rows)
        widths[col] = min(max(len(col), max_cell), 70)

    header = " | ".join(col.ljust(widths[col]) for col in columns)
    divider = "-+-".join("-" * widths[col] for col in columns)
    print(header)
    print(divider)
    for row in rows:
        parts: list[str] = []
        for col in columns:
            text = _format_cell(row.get(col))
            if len(text) > widths[col]:
                text = text[: max(0, widths[col] - 3)] + "..."
            parts.append(text.ljust(widths[col]))
        print(" | ".join(parts))


def _emit_text(data: Any) -> None:
    if isinstance(data, dict):
        for key, value in data.items():
            rendered = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
            print(f"{key}: {rendered}")
        return
    if isinstance(data, list):
        if not data:
            print("(empty)")
            return
        for idx, item in enumerate(data, start=1):
            if isinstance(item, dict):
                title = item.get("title") or item.get("id") or item.get("url") or f"item-{idx}"
                print(f"{idx}. {title}")
            else:
                print(f"{idx}. {item}")
        return
    print(str(data))


def _emit_json(data: Any, *, sort_keys: bool) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=sort_keys))


def _emit_human_output(data: Any, output_format: str) -> None:
    resolved = output_format
    if output_format == "auto":
        if sys.stdout.isatty():
            resolved = "table" if _is_tabular_list(data) else "text"
        else:
            resolved = "json"

    if resolved == "json":
        _emit_json(data, sort_keys=False)
        return
    if resolved == "table":
        if _is_tabular_list(data):
            _emit_table(data)
        else:
            _emit_text(data)
        return
    _emit_text(data)


def _sanitize_schema_part(value: str | None) -> str:
    return (value or "").replace("-", "_").strip("_")


def _schema_for_args(args: argparse.Namespace) -> str:
    if getattr(args, "system", None) != "klms":
        return "kaist.cli.generic.v1"
    group = _sanitize_schema_part(getattr(args, "group", "unknown")) or "unknown"
    action = _sanitize_schema_part(getattr(args, "action", None))
    if group in {"config", "auth", "dev"} and action:
        return f"kaist.klms.{group}.{action}.v1"
    return f"kaist.klms.{group}.v1"


def _infer_source(data: Any) -> str:
    if isinstance(data, dict):
        src = data.get("source")
        if isinstance(src, str) and src.strip():
            return src
        if isinstance(data.get("recommended_endpoints"), list):
            return "api"
        if isinstance(data.get("auth_mode"), str):
            return "html"
        return "mixed"

    if isinstance(data, list):
        sources = {
            str(item.get("source")).strip()
            for item in data
            if isinstance(item, dict) and isinstance(item.get("source"), str) and str(item.get("source")).strip()
        }
        if not sources:
            return "mixed"
        if len(sources) == 1:
            return next(iter(sources))
        return "mixed"

    return "mixed"


def _extract_cursor_fields(data: Any) -> tuple[str | None, str | None]:
    if not isinstance(data, dict):
        return None, None
    cur = data.get("cursor")
    nxt = data.get("next_cursor")
    return (str(cur) if isinstance(cur, str) else None, str(nxt) if isinstance(nxt, str) else None)


def _command_label(args: argparse.Namespace) -> str:
    parts = [str(getattr(args, "system", "")), str(getattr(args, "group", "")), str(getattr(args, "action", ""))]
    return " ".join(p for p in parts if p and p != "None")


def _success_envelope(args: argparse.Namespace, data: Any) -> dict[str, Any]:
    cursor, next_cursor = _extract_cursor_fields(data)
    return {
        "schema": _schema_for_args(args),
        "ok": True,
        "generated_at": _utc_now_iso(),
        "meta": {
            "source": _infer_source(data),
            "cursor": cursor,
            "next_cursor": next_cursor,
            "command": _command_label(args),
        },
        "data": data,
    }


def _classify_error(exc: Exception) -> CliErrorDescriptor:
    msg = str(exc)
    msg_l = msg.lower()
    name = exc.__class__.__name__

    if name == "KlmsAuthError":
        return CliErrorDescriptor("AUTH_EXPIRED", 10, True, "kaist klms auth login")

    if isinstance(exc, FileNotFoundError):
        if "config" in msg_l:
            return CliErrorDescriptor("CONFIG_INVALID", 40, False, "kaist klms config set --base-url https://klms.kaist.ac.kr")
        if "login state" in msg_l or "storage state" in msg_l or "profile" in msg_l:
            return CliErrorDescriptor("AUTH_MISSING", 10, True, "kaist klms auth login")
        return CliErrorDescriptor("NOT_FOUND", 50, False, None)

    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return CliErrorDescriptor("NETWORK_TIMEOUT", 20, True, "retry the command")

    if isinstance(exc, ConnectionError):
        return CliErrorDescriptor("NETWORK_UNAVAILABLE", 20, True, "check network and retry")

    if isinstance(exc, ValueError):
        if any(token in msg_l for token in ["base_url", "config", "must be a list", "dashboard_path"]):
            return CliErrorDescriptor("CONFIG_INVALID", 40, False, "kaist klms config show")
        if any(token in msg_l for token in ["response shape", "payload", "ajax"]):
            return CliErrorDescriptor("API_SHAPE_CHANGED", 30, True, "retry or run kaist klms dev discover-api")
        if any(token in msg_l for token in ["parse", "extract", "selector"]):
            return CliErrorDescriptor("PARSE_DRIFT", 30, True, "retry or run kaist klms dev fetch-html")

    if any(token in msg_l for token in ["ssologin", "re-authenticate", "login state not found", "notloggedin"]):
        return CliErrorDescriptor("AUTH_EXPIRED", 10, True, "kaist klms auth login")

    if any(token in msg_l for token in ["timeout", "timed out"]):
        return CliErrorDescriptor("NETWORK_TIMEOUT", 20, True, "retry the command")

    return CliErrorDescriptor("INTERNAL", 50, False, None)


def _error_envelope(args: argparse.Namespace, descriptor: CliErrorDescriptor, message: str) -> dict[str, Any]:
    return {
        "schema": _schema_for_args(args),
        "ok": False,
        "generated_at": _utc_now_iso(),
        "error": {
            "code": descriptor.code,
            "message": message,
            "retryable": descriptor.retryable,
            "hint": descriptor.hint,
        },
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kaist",
        description="CLI for KAIST systems.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--debug", action="store_true", help="Print traceback on failures.")
    parser.add_argument(
        "--format",
        choices=["auto", "json", "table", "text"],
        default="auto",
        help="Output format. auto=table/text in TTY and json in non-TTY.",
    )
    parser.add_argument(
        "--agent",
        action="store_true",
        help="Agent mode: force strict JSON envelopes and machine-stable output.",
    )

    top = parser.add_subparsers(dest="system", required=True)
    klms_parser = top.add_parser("klms", help="KAIST Learning Management System")
    klms_sub = klms_parser.add_subparsers(dest="group", required=True)

    config = klms_sub.add_parser("config", help="Manage KLMS config")
    config_sub = config.add_subparsers(dest="action", required=True)

    config_set = config_sub.add_parser("set", help="Create or update KLMS config")
    config_set.add_argument("--base-url", help='KLMS base URL, e.g. "https://klms.kaist.ac.kr"')
    config_set.add_argument("--dashboard-path", help='Dashboard path (e.g. "/my/")')
    config_set.add_argument("--course-id", action="append", dest="course_ids", help="Course ID (repeatable)")
    config_set.add_argument(
        "--notice-board-id",
        action="append",
        dest="notice_board_ids",
        help="Notice board ID (repeatable)",
    )
    config_set.add_argument(
        "--exclude-course-title-pattern",
        action="append",
        dest="exclude_course_title_patterns",
        help="Regex filter for noisy/non-course dashboard cards (repeatable)",
    )
    config_set.add_argument(
        "--replace",
        action="store_true",
        help="Overwrite unspecified fields instead of merging with existing config.",
    )

    config_sub.add_parser("show", help="Show config and auth status")

    auth = klms_sub.add_parser("auth", help="Manage KLMS authentication")
    auth_sub = auth.add_subparsers(dest="action", required=True)
    auth_login = auth_sub.add_parser("login", help="Interactive browser login bootstrap")
    auth_login.add_argument("--base-url", help="Optional URL override for login bootstrap")
    auth_status = auth_sub.add_parser("status", help="Inspect auth/config status")
    auth_status.add_argument("--no-validate", action="store_true", help="Skip online validation probe")

    courses = klms_sub.add_parser("courses", help="List courses")
    courses.add_argument("--include-all", action="store_true", help="Include noisy/non-course dashboard items")
    courses.add_argument("--no-enrich", action="store_true", help="Skip per-course metadata fetches")

    assignments = klms_sub.add_parser("assignments", help="List assignments")
    assignments.add_argument("--course-id", help="Single course ID; omit for all discovered courses")

    notices = klms_sub.add_parser("notices", help="List notices")
    notices.add_argument("--notice-board-id", help="Single notice board ID; omit for configured/discovered boards")
    notices.add_argument("--max-pages", type=int, default=1, help="Maximum pages per board")
    notices.add_argument("--stop-post-id", help="Stop paging when this notice post ID is reached")

    files = klms_sub.add_parser("files", help="List non-video materials/files")
    files.add_argument("--course-id", help="Single course ID; omit for all discovered courses")

    download = klms_sub.add_parser("download", help="Download one material file")
    download.add_argument("url", help="KLMS-relative path or absolute URL")
    download.add_argument("--filename", help="Optional filename override")
    download.add_argument("--subdir", help='Relative destination under files root (e.g. "2026 Spring/CS370")')
    download.add_argument("--if-exists", choices=["skip", "overwrite"], default="skip")

    sync = klms_sub.add_parser("sync", help="Incremental snapshot sync")
    sync.add_argument("--dry-run", action="store_true", help="Compute diff only; do not update snapshot")
    sync.add_argument("--max-notice-pages", type=int, default=3, help="Maximum notice pages per board")

    dev = klms_sub.add_parser("dev", help="Experimental and debugging commands")
    dev_sub = dev.add_subparsers(dest="action", required=True)

    fetch_html = dev_sub.add_parser("fetch-html", help="Fetch raw HTML for selector debugging")
    fetch_html.add_argument("path_or_url", help="KLMS-relative path or absolute URL")

    extract = dev_sub.add_parser("extract", help="Extract regex snippets from fetched HTML")
    extract.add_argument("path_or_url", help="KLMS-relative path or absolute URL")
    extract.add_argument("pattern", help="Regex pattern")
    extract.add_argument("--max-matches", type=int, default=20, help="Max matches to return")
    extract.add_argument("--context-chars", type=int, default=120, help="Context chars around each match")

    courses_api = dev_sub.add_parser("courses-api", help="Experimental AJAX-based course listing")
    courses_api.add_argument("--include-all", action="store_true", help="Include noisy/non-course dashboard items")
    courses_api.add_argument("--limit", type=int, default=50, help="Max courses requested from AJAX endpoint")

    dev_sub.add_parser("term", help="Get current term from dashboard")

    course_info = dev_sub.add_parser("course-info", help="Get course metadata")
    course_info.add_argument("course_id", help="Course ID")

    discover_api = dev_sub.add_parser("discover-api", help="Discover internal KLMS XHR/fetch endpoints")
    discover_api.add_argument("--max-courses", type=int, default=2, help="Max courses to sample")
    discover_api.add_argument("--max-notice-boards", type=int, default=2, help="Max notice boards to sample")

    map_api = dev_sub.add_parser("map-api", help="Classify discovered endpoints into CLI-use candidates")
    map_api.add_argument(
        "--report-path",
        help="Optional path to endpoint discovery report (defaults to private endpoint_discovery.json)",
    )

    return parser


def _dispatch_klms(args: argparse.Namespace) -> Any:
    from . import klms

    group = args.group
    if group == "config":
        if args.action == "set":
            return klms.klms_configure(
                args.base_url,
                dashboard_path=args.dashboard_path,
                course_ids=args.course_ids,
                notice_board_ids=args.notice_board_ids,
                exclude_course_title_patterns=args.exclude_course_title_patterns,
                merge_existing=not args.replace,
            )
        if args.action == "show":
            return _run_async(klms.klms_status(validate=False))

    if group == "auth":
        if args.action == "login":
            return klms.klms_bootstrap_login(args.base_url)
        if args.action == "status":
            return _run_async(klms.klms_status(validate=not args.no_validate))

    if group == "courses":
        return _run_klms_async(
            klms.klms_list_courses(
                include_all=args.include_all,
                enrich=not args.no_enrich,
            )
        )

    if group == "assignments":
        return _run_klms_async(klms.klms_list_assignments(course_id=args.course_id))

    if group == "notices":
        return _run_klms_async(
            klms.klms_list_notices(
                notice_board_id=args.notice_board_id,
                max_pages=args.max_pages,
                stop_post_id=args.stop_post_id,
            )
        )

    if group == "files":
        return _run_klms_async(klms.klms_list_files(course_id=args.course_id))

    if group == "download":
        return _run_klms_async(
            klms.klms_download_file(
                args.url,
                filename=args.filename,
                subdir=args.subdir,
                if_exists=args.if_exists,
            )
        )

    if group == "sync":
        return _run_klms_async(
            klms.klms_sync_snapshot(
                update=not args.dry_run,
                max_notice_pages=args.max_notice_pages,
            )
        )

    if group == "dev":
        if args.action == "fetch-html":
            return _run_klms_async(klms.klms_fetch_html(args.path_or_url))
        if args.action == "extract":
            return _run_klms_async(
                klms.klms_extract_matches(
                    args.path_or_url,
                    args.pattern,
                    max_matches=args.max_matches,
                    context_chars=args.context_chars,
                )
            )
        if args.action == "courses-api":
            return _run_klms_async(
                klms.klms_list_courses_api(
                    include_all=args.include_all,
                    limit=args.limit,
                )
            )
        if args.action == "term":
            return _run_klms_async(klms.klms_get_current_term())
        if args.action == "course-info":
            return _run_klms_async(klms.klms_get_course_info(args.course_id))
        if args.action == "discover-api":
            return _run_klms_async(
                klms.klms_discover_api(
                    max_courses=args.max_courses,
                    max_notice_boards=args.max_notice_boards,
                )
            )
        if args.action == "map-api":
            return klms.klms_map_api(report_path=args.report_path)

    raise ValueError(f"Unknown KLMS command group/action: {group}/{getattr(args, 'action', None)}")


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    output_format = "json" if args.agent else args.format
    json_mode = output_format == "json"

    try:
        if args.system != "klms":
            raise ValueError(f"Unsupported system: {args.system}")
        result = _dispatch_klms(args)
        if json_mode:
            _emit_json(_success_envelope(args, result), sort_keys=args.agent)
        else:
            _emit_human_output(result, output_format)
        return 0
    except KeyboardInterrupt:
        if json_mode:
            descriptor = CliErrorDescriptor("INTERNAL", 130, True, "retry command")
            _emit_json(_error_envelope(args, descriptor, "Interrupted."), sort_keys=args.agent)
        else:
            print("Interrupted.", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001
        descriptor = _classify_error(exc)
        if args.debug:
            traceback.print_exc(file=sys.stderr)
        if json_mode:
            _emit_json(_error_envelope(args, descriptor, str(exc)), sort_keys=args.agent)
        else:
            print(f"error [{descriptor.code.lower()}]: {exc}", file=sys.stderr)
        return descriptor.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
