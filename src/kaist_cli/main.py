from __future__ import annotations

import argparse
import asyncio
import json
import sys
import textwrap
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


class _HelpFormatter(argparse.ArgumentDefaultsHelpFormatter, argparse.RawDescriptionHelpFormatter):
    pass


def _dedent(text: str) -> str:
    return textwrap.dedent(text).strip()


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
        description=_dedent(
            """
            CLI for KAIST systems.

            Quick start (KLMS):
              1) kaist klms config set --base-url https://klms.kaist.ac.kr
              2) kaist klms auth login
              3) kaist klms inbox --limit 20

            Use `kaist klms <command> -h` for command-specific examples.
            """
        ),
        epilog=_dedent(
            """
            Common examples:
              kaist klms auth status
              kaist klms courses --limit 10
              kaist --agent klms notices --max-pages 2 --limit 50
            """
        ),
        formatter_class=_HelpFormatter,
    )
    parser.add_argument("--debug", action="store_true", help="Print traceback on failures.")
    parser.add_argument(
        "--format",
        choices=["auto", "json", "table", "text"],
        default="auto",
        help="Output format. auto selects table/text in TTY and json in non-TTY.",
    )
    parser.add_argument(
        "--agent",
        action="store_true",
        help="Agent mode. Forces strict JSON envelopes and deterministic key ordering.",
    )

    top = parser.add_subparsers(dest="system", required=True, title="Systems", metavar="SYSTEM")
    klms_parser = top.add_parser(
        "klms",
        help="KAIST Learning Management System",
        description=_dedent(
            """
            KLMS read-only interface.

            Stable workflow groups:
              config, auth, courses, assignments, notices, files, inbox, download, sync

            Experimental/debug:
              dev
            """
        ),
        epilog=_dedent(
            """
            Examples:
              kaist klms auth status
              kaist klms assignments --since 2026-02-01T00:00 --limit 20
              kaist klms inbox --limit 30
              kaist klms dev discover-api --max-courses 3 --max-notice-boards 2
            """
        ),
        formatter_class=_HelpFormatter,
    )
    klms_sub = klms_parser.add_subparsers(dest="group", required=True, title="KLMS Commands", metavar="COMMAND")

    config = klms_sub.add_parser(
        "config",
        help="Manage local KLMS configuration",
        description="Create/update local KLMS config used by all commands.",
        epilog=_dedent(
            """
            Examples:
              kaist klms config set --base-url https://klms.kaist.ac.kr
              kaist klms config set --course-id 180871 --course-id 178434
              kaist klms config show
            """
        ),
        formatter_class=_HelpFormatter,
    )
    config_sub = config.add_subparsers(dest="action", required=True, title="Config Commands", metavar="ACTION")

    config_set = config_sub.add_parser(
        "set",
        help="Create or update KLMS config",
        description="Write config.toml. By default, merges unspecified fields from existing config.",
        formatter_class=_HelpFormatter,
    )
    config_set.add_argument(
        "--base-url",
        metavar="URL",
        help='KLMS base URL, for example "https://klms.kaist.ac.kr".',
    )
    config_set.add_argument("--dashboard-path", metavar="PATH", help='Dashboard path, for example "/my/".')
    config_set.add_argument("--course-id", action="append", dest="course_ids", metavar="ID", help="Course ID (repeatable).")
    config_set.add_argument(
        "--notice-board-id",
        action="append",
        dest="notice_board_ids",
        metavar="ID",
        help="Notice board ID (repeatable).",
    )
    config_set.add_argument(
        "--exclude-course-title-pattern",
        action="append",
        dest="exclude_course_title_patterns",
        metavar="REGEX",
        help="Regex filter for noisy/non-course dashboard cards (repeatable).",
    )
    config_set.add_argument(
        "--replace",
        action="store_true",
        help="Overwrite unspecified fields instead of merging existing config.",
    )

    config_sub.add_parser(
        "show",
        help="Show config/auth summary",
        description="Display config and local auth artifact status without online validation.",
        formatter_class=_HelpFormatter,
    )

    auth = klms_sub.add_parser(
        "auth",
        help="Manage KLMS authentication/session",
        description="Authenticate and diagnose session state stored under ~/.kaist-cli/private/klms.",
        epilog=_dedent(
            """
            Examples:
              kaist klms auth login
              kaist klms auth status
              kaist klms auth refresh
              kaist klms auth doctor
            """
        ),
        formatter_class=_HelpFormatter,
    )
    auth_sub = auth.add_subparsers(dest="action", required=True, title="Auth Commands", metavar="ACTION")
    auth_login = auth_sub.add_parser(
        "login",
        help="Interactive browser login bootstrap",
        description="Open a browser, sign in to KLMS, then persist profile/storage_state artifacts.",
        formatter_class=_HelpFormatter,
    )
    auth_login.add_argument("--base-url", metavar="URL", help="Optional URL override for login bootstrap.")
    auth_status = auth_sub.add_parser(
        "status",
        help="Inspect auth/config status",
        description="Show auth artifacts and optionally verify live authentication against dashboard.",
        formatter_class=_HelpFormatter,
    )
    auth_status.add_argument("--no-validate", action="store_true", help="Skip online validation probe")
    auth_refresh = auth_sub.add_parser(
        "refresh",
        help="Re-run login flow and verify refreshed session",
        description="Run interactive login and immediately verify the refreshed session.",
        formatter_class=_HelpFormatter,
    )
    auth_refresh.add_argument("--base-url", metavar="URL", help="Optional URL override for login bootstrap.")
    auth_refresh.add_argument("--no-validate", action="store_true", help="Skip post-refresh validation probe")
    auth_doctor = auth_sub.add_parser(
        "doctor",
        help="Run auth/session diagnostics",
        description="Run local artifact/config checks and optional live auth validation.",
        formatter_class=_HelpFormatter,
    )
    auth_doctor.add_argument("--no-validate", action="store_true", help="Skip online validation probe")

    courses = klms_sub.add_parser(
        "courses",
        help="List courses",
        description="List courses discovered from dashboard (with config fallback).",
        epilog="Example: kaist klms courses --no-enrich --limit 20",
        formatter_class=_HelpFormatter,
    )
    courses.add_argument("--include-all", action="store_true", help="Include noisy/non-course dashboard items")
    courses.add_argument("--no-enrich", action="store_true", help="Skip per-course metadata fetches")
    courses.add_argument("--limit", type=int, metavar="N", help="Maximum number of courses to return.")

    assignments = klms_sub.add_parser(
        "assignments",
        help="List assignments",
        description="List assignments (API-first with HTML fallback).",
        epilog="Example: kaist klms assignments --course-id 180871 --since 2026-02-01T00:00 --limit 30",
        formatter_class=_HelpFormatter,
    )
    assignments.add_argument("--course-id", metavar="ID", help="Single course ID; omit for all discovered courses.")
    assignments.add_argument(
        "--since",
        dest="since_iso",
        metavar="ISO",
        help="Only include assignments with due_iso >= this ISO timestamp.",
    )
    assignments.add_argument("--limit", type=int, metavar="N", help="Maximum number of assignments to return.")

    notices = klms_sub.add_parser(
        "notices",
        help="List notices",
        description="List course notices (API-first with HTML fallback).",
        epilog="Example: kaist klms notices --notice-board-id 1183822 --max-pages 2 --limit 50",
        formatter_class=_HelpFormatter,
    )
    notices.add_argument("--notice-board-id", metavar="ID", help="Single notice board ID; omit for configured/discovered boards.")
    notices.add_argument("--max-pages", type=int, default=1, metavar="N", help="Maximum pages per board.")
    notices.add_argument("--stop-post-id", metavar="ID", help="Stop paging when this notice post ID is reached.")
    notices.add_argument(
        "--since",
        dest="since_iso",
        metavar="ISO",
        help="Only include notices with posted_iso >= this ISO timestamp.",
    )
    notices.add_argument("--limit", type=int, metavar="N", help="Maximum number of notices to return.")

    files = klms_sub.add_parser(
        "files",
        help="List non-video materials/files",
        description="List non-video file/material links from course resources.",
        epilog="Example: kaist klms files --course-id 180871 --limit 40",
        formatter_class=_HelpFormatter,
    )
    files.add_argument("--course-id", metavar="ID", help="Single course ID; omit for all discovered courses.")
    files.add_argument("--limit", type=int, metavar="N", help="Maximum number of files to return.")

    inbox = klms_sub.add_parser(
        "inbox",
        help="Blended feed of assignments, notices, and files",
        description="Build a daily feed by combining assignments, notices, and files.",
        epilog="Example: kaist klms inbox --since 2026-02-01T00:00 --limit 30",
        formatter_class=_HelpFormatter,
    )
    inbox.add_argument("--limit", type=int, default=30, metavar="N", help="Maximum number of inbox items to return.")
    inbox.add_argument("--max-notice-pages", type=int, default=1, metavar="N", help="Maximum notice pages per board.")
    inbox.add_argument("--since", dest="since_iso", metavar="ISO", help="Filter assignments/notices by ISO timestamp.")

    download = klms_sub.add_parser(
        "download",
        help="Download one material file",
        description="Download a file URL into ~/.kaist-cli/files/klms.",
        epilog='Example: kaist klms download "https://.../pluginfile.php/..." --subdir "2026 Spring/CS30000"',
        formatter_class=_HelpFormatter,
    )
    download.add_argument("url", metavar="URL", help="KLMS-relative path or absolute URL.")
    download.add_argument("--filename", metavar="NAME", help="Optional filename override.")
    download.add_argument("--subdir", metavar="DIR", help='Relative destination under files root (for example "2026 Spring/CS370").')
    download.add_argument("--if-exists", choices=["skip", "overwrite"], default="skip", help="Behavior when destination file exists.")

    sync = klms_sub.add_parser(
        "sync",
        help="Incremental snapshot sync",
        description="Diff current assignments/notices/files against saved snapshot.",
        epilog="Example: kaist klms sync --dry-run --max-notice-pages 2",
        formatter_class=_HelpFormatter,
    )
    sync.add_argument("--dry-run", action="store_true", help="Compute diff only; do not update snapshot")
    sync.add_argument("--max-notice-pages", type=int, default=3, metavar="N", help="Maximum notice pages per board.")

    dev = klms_sub.add_parser(
        "dev",
        help="Experimental and debugging commands",
        description="Unstable tooling for endpoint discovery and parser debugging.",
        formatter_class=_HelpFormatter,
    )
    dev_sub = dev.add_subparsers(dest="action", required=True, title="Dev Commands", metavar="ACTION")

    fetch_html = dev_sub.add_parser(
        "fetch-html",
        help="Fetch raw HTML for selector debugging",
        description="Fetch a page and return raw HTML using current auth session.",
        formatter_class=_HelpFormatter,
    )
    fetch_html.add_argument("path_or_url", metavar="PATH_OR_URL", help="KLMS-relative path or absolute URL.")

    extract = dev_sub.add_parser(
        "extract",
        help="Extract regex snippets from fetched HTML",
        description="Fetch HTML and return regex match snippets for debugging selectors/patterns.",
        formatter_class=_HelpFormatter,
    )
    extract.add_argument("path_or_url", metavar="PATH_OR_URL", help="KLMS-relative path or absolute URL.")
    extract.add_argument("pattern", metavar="REGEX", help="Regex pattern.")
    extract.add_argument("--max-matches", type=int, default=20, metavar="N", help="Maximum matches to return.")
    extract.add_argument("--context-chars", type=int, default=120, metavar="N", help="Context chars around each match.")

    courses_api = dev_sub.add_parser(
        "courses-api",
        help="Experimental AJAX-based course listing",
        description="Force the AJAX-based course listing path for debugging/perf checks.",
        formatter_class=_HelpFormatter,
    )
    courses_api.add_argument("--include-all", action="store_true", help="Include noisy/non-course dashboard items")
    courses_api.add_argument("--limit", type=int, default=50, metavar="N", help="Maximum courses requested from AJAX endpoint.")

    dev_sub.add_parser(
        "term",
        help="Get current term from dashboard",
        description="Extract currently selected term from dashboard controls.",
        formatter_class=_HelpFormatter,
    )

    course_info = dev_sub.add_parser(
        "course-info",
        help="Get course metadata",
        description="Fetch metadata for one course id (title/code/url).",
        formatter_class=_HelpFormatter,
    )
    course_info.add_argument("course_id", metavar="ID", help="Course ID.")

    discover_api = dev_sub.add_parser(
        "discover-api",
        help="Discover internal KLMS XHR/fetch endpoints",
        description="Observe XHR/fetch traffic while visiting representative pages.",
        epilog="Example: kaist klms dev discover-api --max-courses 5 --max-notice-boards 5",
        formatter_class=_HelpFormatter,
    )
    discover_api.add_argument("--max-courses", type=int, default=2, metavar="N", help="Maximum courses to sample.")
    discover_api.add_argument("--max-notice-boards", type=int, default=2, metavar="N", help="Maximum notice boards to sample.")

    map_api = dev_sub.add_parser(
        "map-api",
        help="Classify discovered endpoints into CLI-use candidates",
        description="Read discovery report and build categorized endpoint map with recommendations.",
        formatter_class=_HelpFormatter,
    )
    map_api.add_argument(
        "--report-path",
        metavar="PATH",
        help="Optional path to endpoint discovery report (default: ~/.kaist-cli/private/klms/endpoint_discovery.json).",
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
        if args.action == "refresh":
            return klms.klms_refresh_auth(args.base_url, validate=not args.no_validate)
        if args.action == "doctor":
            return _run_async(klms.klms_auth_doctor(validate=not args.no_validate))

    if group == "courses":
        return _run_klms_async(
            klms.klms_list_courses(
                include_all=args.include_all,
                enrich=not args.no_enrich,
                limit=args.limit,
            )
        )

    if group == "assignments":
        return _run_klms_async(
            klms.klms_list_assignments(
                course_id=args.course_id,
                limit=args.limit,
                since_iso=args.since_iso,
            )
        )

    if group == "notices":
        return _run_klms_async(
            klms.klms_list_notices(
                notice_board_id=args.notice_board_id,
                max_pages=args.max_pages,
                stop_post_id=args.stop_post_id,
                limit=args.limit,
                since_iso=args.since_iso,
            )
        )

    if group == "files":
        return _run_klms_async(klms.klms_list_files(course_id=args.course_id, limit=args.limit))

    if group == "inbox":
        return _run_klms_async(
            klms.klms_inbox(
                limit=args.limit,
                max_notice_pages=args.max_notice_pages,
                since_iso=args.since_iso,
            )
        )

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
