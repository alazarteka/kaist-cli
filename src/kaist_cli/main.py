from __future__ import annotations

import argparse
import asyncio
import json
import sys
import traceback
from typing import Any


def _emit_json(data: Any) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=False))


def _run_async(coro: Any) -> Any:
    return asyncio.run(coro)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kaist", description="CLI for KAIST systems.")
    parser.add_argument("--debug", action="store_true", help="Print traceback on failures.")

    top = parser.add_subparsers(dest="system", required=True)
    klms_parser = top.add_parser("klms", help="KAIST Learning Management System")
    klms_sub = klms_parser.add_subparsers(dest="command", required=True)

    configure = klms_sub.add_parser("configure", help="Write KLMS config")
    configure.add_argument("--base-url", required=True, help='KLMS base URL, e.g. "https://klms.kaist.ac.kr"')
    configure.add_argument("--dashboard-path", help='Dashboard path (default: "/my/")')
    configure.add_argument("--course-id", action="append", dest="course_ids", help="Optional course ID (repeatable)")
    configure.add_argument(
        "--notice-board-id",
        action="append",
        dest="notice_board_ids",
        help="Optional notice board ID (repeatable)",
    )
    configure.add_argument(
        "--exclude-course-title-pattern",
        action="append",
        dest="exclude_course_title_patterns",
        help="Regex title filter for noisy non-course cards (repeatable)",
    )
    configure.add_argument(
        "--replace",
        action="store_true",
        help="Do not merge with existing config; overwrite unspecified fields.",
    )

    login = klms_sub.add_parser("login", help="Bootstrap interactive KLMS login and save session cookies")
    login.add_argument("--base-url", help="Optional URL override for login bootstrap")

    status = klms_sub.add_parser("status", help="Check config/session status")
    status.add_argument("--no-validate", action="store_true", help="Skip online auth validation probe")

    fetch_html = klms_sub.add_parser("fetch-html", help="Fetch raw HTML for debugging selectors")
    fetch_html.add_argument("path_or_url", help="KLMS-relative path or absolute URL")

    extract = klms_sub.add_parser("extract", help="Extract regex snippets from fetched HTML")
    extract.add_argument("path_or_url", help="KLMS-relative path or absolute URL")
    extract.add_argument("pattern", help="Regex pattern")
    extract.add_argument("--max-matches", type=int, default=20)
    extract.add_argument("--context-chars", type=int, default=120)

    courses = klms_sub.add_parser("courses", help="List courses")
    courses.add_argument("--include-all", action="store_true", help="Include noisy/non-course dashboard items")

    klms_sub.add_parser("term", help="Get current term from dashboard")

    course_info = klms_sub.add_parser("course-info", help="Get course metadata")
    course_info.add_argument("course_id")

    assignments = klms_sub.add_parser("assignments", help="List assignments")
    assignments.add_argument("--course-id", help="Single course ID; omit for all discovered courses")

    notices = klms_sub.add_parser("notices", help="List notices")
    notices.add_argument(
        "--board-id",
        dest="board_id",
        help="Courseboard ID (if omitted, uses config.notice_board_ids or autodiscovery)",
    )
    notices.add_argument("--max-pages", type=int, default=1)
    notices.add_argument("--stop-bwid", help="Stop paging when this notice post ID is reached")

    files = klms_sub.add_parser("files", help="List non-video materials/files")
    files.add_argument("--course-id", help="Single course ID; omit for all discovered courses")

    sync = klms_sub.add_parser("sync", help="Incremental snapshot sync")
    sync.add_argument("--no-update", action="store_true", help="Compute diff only, do not write snapshot")
    sync.add_argument("--max-notice-pages", type=int, default=3)

    download = klms_sub.add_parser("download", help="Download a material file")
    download.add_argument("url", help="KLMS-relative path or absolute URL")
    download.add_argument("--filename")
    download.add_argument("--subdir", help='Optional relative destination under files root (e.g. "2026 Spring/CS370")')
    download.add_argument("--if-exists", choices=["skip", "overwrite"], default="skip")

    return parser


def _dispatch_klms(args: argparse.Namespace) -> Any:
    from . import klms

    if args.command == "configure":
        return klms.klms_configure(
            args.base_url,
            dashboard_path=args.dashboard_path,
            course_ids=args.course_ids,
            notice_board_ids=args.notice_board_ids,
            exclude_course_title_patterns=args.exclude_course_title_patterns,
            merge_existing=not args.replace,
        )
    if args.command == "login":
        return klms.klms_bootstrap_login(args.base_url)
    if args.command == "status":
        return _run_async(klms.klms_status(validate=not args.no_validate))
    if args.command == "fetch-html":
        return _run_async(klms.klms_fetch_html(args.path_or_url))
    if args.command == "extract":
        return _run_async(
            klms.klms_extract_matches(
                args.path_or_url,
                args.pattern,
                max_matches=args.max_matches,
                context_chars=args.context_chars,
            )
        )
    if args.command == "courses":
        return _run_async(klms.klms_list_courses(include_all=args.include_all))
    if args.command == "term":
        return _run_async(klms.klms_get_current_term())
    if args.command == "course-info":
        return _run_async(klms.klms_get_course_info(args.course_id))
    if args.command == "assignments":
        return _run_async(klms.klms_list_assignments(course_id=args.course_id))
    if args.command == "notices":
        return _run_async(
            klms.klms_list_notices(
                course_id=args.board_id,
                max_pages=args.max_pages,
                stop_bwid=args.stop_bwid,
            )
        )
    if args.command == "files":
        return _run_async(klms.klms_list_files(course_id=args.course_id))
    if args.command == "sync":
        return _run_async(
            klms.klms_sync_snapshot(
                update=not args.no_update,
                max_notice_pages=args.max_notice_pages,
            )
        )
    if args.command == "download":
        return _run_async(
            klms.klms_download_file(
                args.url,
                filename=args.filename,
                subdir=args.subdir,
                if_exists=args.if_exists,
            )
        )

    raise ValueError(f"Unknown KLMS command: {args.command}")


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        if args.system != "klms":
            raise ValueError(f"Unsupported system: {args.system}")
        result = _dispatch_klms(args)
        _emit_json(result)
        return 0
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001
        if args.debug:
            traceback.print_exc()
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
