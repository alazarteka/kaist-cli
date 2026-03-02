from __future__ import annotations

import argparse
import asyncio
import textwrap
from typing import Any, Callable

from ... import klms as legacy
from ...core.contracts import SystemAdapter
from . import auth as klms_auth
from . import config as klms_config
from .services import assignments, courses, download, files, inbox, notices, sync


class _HelpFormatter(argparse.ArgumentDefaultsHelpFormatter, argparse.RawDescriptionHelpFormatter):
    pass


def _dedent(text: str) -> str:
    return textwrap.dedent(text).strip()


class KlmsAdapter(SystemAdapter):
    system_name = "klms"

    def register(self, top_subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
        klms_parser = top_subparsers.add_parser(
            "klms",
            help="KAIST Learning Management System",
            description=_dedent(
                """
                KLMS read-only interface.

                Stable workflow groups:
                  config, auth, list, get, sync, inbox

                Experimental/debug:
                  dev
                """
            ),
            formatter_class=_HelpFormatter,
        )
        klms_sub = klms_parser.add_subparsers(dest="group", required=True, title="KLMS Commands", metavar="COMMAND")

        self._register_config(klms_sub)
        self._register_auth(klms_sub)
        self._register_list(klms_sub)
        self._register_get(klms_sub)
        self._register_sync(klms_sub)
        self._register_inbox(klms_sub)
        self._register_dev(klms_sub)

    @staticmethod
    def _run_async(coro: Any) -> Any:
        return asyncio.run(coro)

    @staticmethod
    def _run_klms_async(coro_factory: Callable[[], Any]) -> Any:
        async def wrapped() -> Any:
            async with klms_auth.klms_runtime(headless=True, accept_downloads=True):
                return await coro_factory()

        return asyncio.run(wrapped())

    def _register_config(self, klms_sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
        config = klms_sub.add_parser(
            "config",
            help="Manage local KLMS configuration",
            description="Create/update local KLMS config used by all commands.",
            formatter_class=_HelpFormatter,
        )
        config_sub = config.add_subparsers(dest="action", required=True, title="Config Commands", metavar="ACTION")

        config_set = config_sub.add_parser("set", help="Create or update KLMS config", formatter_class=_HelpFormatter)
        config_set.add_argument("--base-url", metavar="URL", help='KLMS base URL, for example "https://klms.kaist.ac.kr".')
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
        config_set.add_argument("--replace", action="store_true", help="Overwrite unspecified fields instead of merging existing config.")
        config_set.set_defaults(
            handler=self._handle_config_set,
            schema_name="kaist.klms.config.set.v1",
            command_path="klms config set",
        )

        config_show = config_sub.add_parser("show", help="Show config/auth summary", formatter_class=_HelpFormatter)
        config_show.set_defaults(
            handler=self._handle_config_show,
            schema_name="kaist.klms.config.show.v1",
            command_path="klms config show",
        )

    def _register_auth(self, klms_sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
        auth = klms_sub.add_parser(
            "auth",
            help="Manage KLMS authentication/session",
            description="Authenticate and diagnose session state stored under ~/.kaist-cli/private/klms.",
            formatter_class=_HelpFormatter,
        )
        auth_sub = auth.add_subparsers(dest="action", required=True, title="Auth Commands", metavar="ACTION")

        auth_login = auth_sub.add_parser("login", help="Interactive browser login bootstrap", formatter_class=_HelpFormatter)
        auth_login.add_argument("--base-url", metavar="URL", help="Optional URL override for login bootstrap.")
        auth_login.set_defaults(
            handler=self._handle_auth_login,
            schema_name="kaist.klms.auth.login.v1",
            command_path="klms auth login",
        )

        auth_status = auth_sub.add_parser("status", help="Inspect auth/config status", formatter_class=_HelpFormatter)
        auth_status.add_argument("--no-validate", action="store_true", help="Skip online validation probe")
        auth_status.set_defaults(
            handler=self._handle_auth_status,
            schema_name="kaist.klms.auth.status.v1",
            command_path="klms auth status",
        )

        auth_refresh = auth_sub.add_parser("refresh", help="Re-run login flow and verify refreshed session", formatter_class=_HelpFormatter)
        auth_refresh.add_argument("--base-url", metavar="URL", help="Optional URL override for login bootstrap.")
        auth_refresh.add_argument("--no-validate", action="store_true", help="Skip post-refresh validation probe")
        auth_refresh.set_defaults(
            handler=self._handle_auth_refresh,
            schema_name="kaist.klms.auth.refresh.v1",
            command_path="klms auth refresh",
        )

        auth_doctor = auth_sub.add_parser("doctor", help="Run auth/session diagnostics", formatter_class=_HelpFormatter)
        auth_doctor.add_argument("--no-validate", action="store_true", help="Skip online validation probe")
        auth_doctor.set_defaults(
            handler=self._handle_auth_doctor,
            schema_name="kaist.klms.auth.doctor.v1",
            command_path="klms auth doctor",
        )

    def _register_list(self, klms_sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
        list_parser = klms_sub.add_parser("list", help="List KLMS resources", formatter_class=_HelpFormatter)
        list_sub = list_parser.add_subparsers(dest="resource", required=True, title="List Resources", metavar="RESOURCE")

        list_courses = list_sub.add_parser("courses", help="List courses", formatter_class=_HelpFormatter)
        list_courses.add_argument("--include-all", action="store_true", help="Include noisy/non-course dashboard items")
        list_courses.add_argument("--no-enrich", action="store_true", help="Skip per-course metadata fetches")
        list_courses.add_argument("--limit", type=int, metavar="N", help="Maximum number of courses to return.")
        list_courses.set_defaults(
            handler=self._handle_list_courses,
            schema_name="kaist.klms.courses.v1",
            command_path="klms list courses",
        )

        list_assignments = list_sub.add_parser("assignments", help="List assignments", formatter_class=_HelpFormatter)
        list_assignments.add_argument("--course-id", metavar="ID", help="Single course ID; omit for all discovered courses.")
        list_assignments.add_argument("--since", dest="since_iso", metavar="ISO", help="Only include assignments with due_iso >= ISO.")
        list_assignments.add_argument("--limit", type=int, metavar="N", help="Maximum number of assignments to return.")
        list_assignments.set_defaults(
            handler=self._handle_list_assignments,
            schema_name="kaist.klms.assignments.v1",
            command_path="klms list assignments",
        )

        list_notices = list_sub.add_parser("notices", help="List notices", formatter_class=_HelpFormatter)
        list_notices.add_argument("--notice-board-id", metavar="ID", help="Single notice board ID; omit for configured/discovered boards.")
        list_notices.add_argument("--max-pages", type=int, default=1, metavar="N", help="Maximum pages per board.")
        list_notices.add_argument("--stop-post-id", metavar="ID", help="Stop paging when this notice post ID is reached.")
        list_notices.add_argument("--since", dest="since_iso", metavar="ISO", help="Only include notices with posted_iso >= ISO.")
        list_notices.add_argument("--limit", type=int, metavar="N", help="Maximum number of notices to return.")
        list_notices.set_defaults(
            handler=self._handle_list_notices,
            schema_name="kaist.klms.notices.v1",
            command_path="klms list notices",
        )

        list_files = list_sub.add_parser("files", help="List non-video materials/files", formatter_class=_HelpFormatter)
        list_files.add_argument("--course-id", metavar="ID", help="Single course ID; omit for all discovered courses.")
        list_files.add_argument("--limit", type=int, metavar="N", help="Maximum number of files to return.")
        list_files.set_defaults(
            handler=self._handle_list_files,
            schema_name="kaist.klms.files.v1",
            command_path="klms list files",
        )

    def _register_get(self, klms_sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
        get_parser = klms_sub.add_parser("get", help="Get one resource", formatter_class=_HelpFormatter)
        get_sub = get_parser.add_subparsers(dest="resource", required=True, title="Get Resources", metavar="RESOURCE")

        get_course = get_sub.add_parser("course", help="Get one course metadata bundle", formatter_class=_HelpFormatter)
        get_course.add_argument("course_id", metavar="ID", help="Course ID")
        get_course.set_defaults(
            handler=self._handle_get_course,
            schema_name="kaist.klms.courses.v1",
            command_path="klms get course",
        )

        get_assignment = get_sub.add_parser("assignment", help="Get one assignment", formatter_class=_HelpFormatter)
        get_assignment.add_argument("assignment_id", metavar="ID", help="Assignment ID")
        get_assignment.add_argument("--course-id", metavar="ID", help="Optional course scope")
        get_assignment.set_defaults(
            handler=self._handle_get_assignment,
            schema_name="kaist.klms.assignments.v1",
            command_path="klms get assignment",
        )

        get_notice = get_sub.add_parser("notice", help="Get one notice", formatter_class=_HelpFormatter)
        get_notice.add_argument("notice_id", metavar="ID", help="Notice ID")
        get_notice.add_argument("--notice-board-id", metavar="ID", help="Optional board scope")
        get_notice.add_argument("--max-pages", type=int, default=3, metavar="N", help="Maximum pages per board to scan")
        get_notice.add_argument("--include-html", action="store_true", help="Include parsed notice body HTML in output.")
        get_notice.set_defaults(
            handler=self._handle_get_notice,
            schema_name="kaist.klms.notices.v1",
            command_path="klms get notice",
        )

        get_file = get_sub.add_parser("file", help="Download one material file", formatter_class=_HelpFormatter)
        get_file.add_argument("id_or_url", metavar="ID_OR_URL", help="KLMS-relative path or absolute URL")
        get_file.add_argument("--filename", metavar="NAME", help="Optional filename override")
        get_file.add_argument("--subdir", metavar="DIR", help="Relative destination under files root")
        get_file.add_argument("--if-exists", choices=["skip", "overwrite"], default="skip", help="Behavior when destination file exists")
        get_file.set_defaults(
            handler=self._handle_get_file,
            schema_name="kaist.klms.download.v1",
            command_path="klms get file",
        )

    def _register_sync(self, klms_sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
        sync_parser = klms_sub.add_parser("sync", help="Snapshot sync operations", formatter_class=_HelpFormatter)
        sync_sub = sync_parser.add_subparsers(dest="sync_action", required=True, title="Sync Commands", metavar="ACTION")

        sync_run = sync_sub.add_parser("run", help="Incremental snapshot sync", formatter_class=_HelpFormatter)
        sync_run.add_argument("--dry-run", action="store_true", help="Compute diff only; do not update snapshot")
        sync_run.add_argument("--max-notice-pages", type=int, default=3, metavar="N", help="Maximum notice pages per board")
        sync_run.set_defaults(
            handler=self._handle_sync_run,
            schema_name="kaist.klms.sync.v1",
            command_path="klms sync run",
        )

        sync_status = sync_sub.add_parser("status", help="Show local sync snapshot status", formatter_class=_HelpFormatter)
        sync_status.set_defaults(
            handler=self._handle_sync_status,
            schema_name="kaist.klms.sync.status.v1",
            command_path="klms sync status",
        )

        sync_reset = sync_sub.add_parser("reset", help="Delete local snapshot state", formatter_class=_HelpFormatter)
        sync_reset.set_defaults(
            handler=self._handle_sync_reset,
            schema_name="kaist.klms.sync.reset.v1",
            command_path="klms sync reset",
        )

    def _register_inbox(self, klms_sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
        inbox_parser = klms_sub.add_parser(
            "inbox",
            help="Blended feed of assignments, notices, and files",
            formatter_class=_HelpFormatter,
        )
        inbox_parser.add_argument("--limit", type=int, default=30, metavar="N", help="Maximum number of inbox items to return.")
        inbox_parser.add_argument("--max-notice-pages", type=int, default=1, metavar="N", help="Maximum notice pages per board.")
        inbox_parser.add_argument("--since", dest="since_iso", metavar="ISO", help="Filter assignments/notices by ISO timestamp.")
        inbox_parser.set_defaults(
            handler=self._handle_inbox,
            schema_name="kaist.klms.inbox.v1",
            command_path="klms inbox",
        )

    def _register_dev(self, klms_sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
        dev = klms_sub.add_parser("dev", help="Experimental and debugging commands", formatter_class=_HelpFormatter)
        dev_sub = dev.add_subparsers(dest="action", required=True, title="Dev Commands", metavar="ACTION")

        fetch_html = dev_sub.add_parser("fetch-html", help="Fetch raw HTML for selector debugging", formatter_class=_HelpFormatter)
        fetch_html.add_argument("path_or_url", metavar="PATH_OR_URL", help="KLMS-relative path or absolute URL.")
        fetch_html.set_defaults(
            handler=self._handle_dev_fetch_html,
            schema_name="kaist.klms.dev.fetch_html.v1",
            command_path="klms dev fetch-html",
        )

        extract = dev_sub.add_parser("extract", help="Extract regex snippets from fetched HTML", formatter_class=_HelpFormatter)
        extract.add_argument("path_or_url", metavar="PATH_OR_URL", help="KLMS-relative path or absolute URL.")
        extract.add_argument("pattern", metavar="REGEX", help="Regex pattern.")
        extract.add_argument("--max-matches", type=int, default=20, metavar="N", help="Maximum matches to return.")
        extract.add_argument("--context-chars", type=int, default=120, metavar="N", help="Context chars around each match.")
        extract.set_defaults(
            handler=self._handle_dev_extract,
            schema_name="kaist.klms.dev.extract.v1",
            command_path="klms dev extract",
        )

        courses_api = dev_sub.add_parser("courses-api", help="Experimental AJAX-based course listing", formatter_class=_HelpFormatter)
        courses_api.add_argument("--include-all", action="store_true", help="Include noisy/non-course dashboard items")
        courses_api.add_argument("--limit", type=int, default=50, metavar="N", help="Maximum courses requested from AJAX endpoint.")
        courses_api.set_defaults(
            handler=self._handle_dev_courses_api,
            schema_name="kaist.klms.dev.courses_api.v1",
            command_path="klms dev courses-api",
        )

        term = dev_sub.add_parser("term", help="Get current term from dashboard", formatter_class=_HelpFormatter)
        term.set_defaults(
            handler=self._handle_dev_term,
            schema_name="kaist.klms.dev.term.v1",
            command_path="klms dev term",
        )

        course_info = dev_sub.add_parser("course-info", help="Get course metadata", formatter_class=_HelpFormatter)
        course_info.add_argument("course_id", metavar="ID", help="Course ID.")
        course_info.set_defaults(
            handler=self._handle_dev_course_info,
            schema_name="kaist.klms.dev.course_info.v1",
            command_path="klms dev course-info",
        )

        discover_api = dev_sub.add_parser("discover-api", help="Discover internal KLMS XHR/fetch endpoints", formatter_class=_HelpFormatter)
        discover_api.add_argument("--max-courses", type=int, default=2, metavar="N", help="Maximum courses to sample.")
        discover_api.add_argument("--max-notice-boards", type=int, default=2, metavar="N", help="Maximum notice boards to sample.")
        discover_api.set_defaults(
            handler=self._handle_dev_discover_api,
            schema_name="kaist.klms.dev.discover_api.v1",
            command_path="klms dev discover-api",
        )

        map_api = dev_sub.add_parser("map-api", help="Classify discovered endpoints", formatter_class=_HelpFormatter)
        map_api.add_argument(
            "--report-path",
            metavar="PATH",
            help="Optional path to endpoint discovery report (default: ~/.kaist-cli/private/klms/endpoint_discovery.json).",
        )
        map_api.set_defaults(
            handler=self._handle_dev_map_api,
            schema_name="kaist.klms.dev.map_api.v1",
            command_path="klms dev map-api",
        )

    def _handle_config_set(self, args: argparse.Namespace) -> dict[str, Any]:
        return klms_config.set_config(
            args.base_url,
            dashboard_path=args.dashboard_path,
            course_ids=args.course_ids,
            notice_board_ids=args.notice_board_ids,
            exclude_course_title_patterns=args.exclude_course_title_patterns,
            merge_existing=not args.replace,
        )

    def _handle_config_show(self, args: argparse.Namespace) -> dict[str, Any]:  # noqa: ARG002
        return klms_config.show_config()

    def _handle_auth_login(self, args: argparse.Namespace) -> dict[str, Any]:
        return klms_auth.login(args.base_url)

    def _handle_auth_status(self, args: argparse.Namespace) -> dict[str, Any]:
        return self._run_async(klms_auth.status(validate=not args.no_validate))

    def _handle_auth_refresh(self, args: argparse.Namespace) -> dict[str, Any]:
        return klms_auth.refresh(args.base_url, validate=not args.no_validate)

    def _handle_auth_doctor(self, args: argparse.Namespace) -> dict[str, Any]:
        return self._run_async(klms_auth.doctor(validate=not args.no_validate))

    def _handle_list_courses(self, args: argparse.Namespace) -> Any:
        return self._run_klms_async(
            lambda: courses.list_courses(include_all=args.include_all, enrich=not args.no_enrich, limit=args.limit)
        )

    def _handle_list_assignments(self, args: argparse.Namespace) -> Any:
        return self._run_klms_async(
            lambda: assignments.list_assignments(course_id=args.course_id, since_iso=args.since_iso, limit=args.limit)
        )

    def _handle_list_notices(self, args: argparse.Namespace) -> Any:
        return self._run_klms_async(
            lambda: notices.list_notices(
                notice_board_id=args.notice_board_id,
                max_pages=args.max_pages,
                stop_post_id=args.stop_post_id,
                since_iso=args.since_iso,
                limit=args.limit,
            )
        )

    def _handle_list_files(self, args: argparse.Namespace) -> Any:
        return self._run_klms_async(lambda: files.list_files(course_id=args.course_id, limit=args.limit))

    def _handle_get_course(self, args: argparse.Namespace) -> Any:
        return self._run_klms_async(lambda: courses.get_course(args.course_id))

    def _handle_get_assignment(self, args: argparse.Namespace) -> Any:
        return self._run_klms_async(lambda: assignments.get_assignment(args.assignment_id, course_id=args.course_id))

    def _handle_get_notice(self, args: argparse.Namespace) -> Any:
        return self._run_klms_async(
            lambda: notices.get_notice(
                args.notice_id,
                notice_board_id=args.notice_board_id,
                max_pages=args.max_pages,
                include_html=args.include_html,
            )
        )

    def _handle_get_file(self, args: argparse.Namespace) -> Any:
        return self._run_klms_async(
            lambda: download.get_file(
                args.id_or_url,
                filename=args.filename,
                subdir=args.subdir,
                if_exists=args.if_exists,
            )
        )

    def _handle_sync_run(self, args: argparse.Namespace) -> Any:
        return self._run_klms_async(lambda: sync.run(update=not args.dry_run, max_notice_pages=args.max_notice_pages))

    def _handle_sync_status(self, args: argparse.Namespace) -> dict[str, Any]:  # noqa: ARG002
        return sync.status()

    def _handle_sync_reset(self, args: argparse.Namespace) -> dict[str, Any]:  # noqa: ARG002
        return sync.reset()

    def _handle_inbox(self, args: argparse.Namespace) -> Any:
        return self._run_klms_async(
            lambda: inbox.list_inbox(limit=args.limit, max_notice_pages=args.max_notice_pages, since_iso=args.since_iso)
        )

    def _handle_dev_fetch_html(self, args: argparse.Namespace) -> Any:
        return self._run_klms_async(lambda: legacy.klms_fetch_html(args.path_or_url))

    def _handle_dev_extract(self, args: argparse.Namespace) -> Any:
        return self._run_klms_async(
            lambda: legacy.klms_extract_matches(
                args.path_or_url,
                args.pattern,
                max_matches=args.max_matches,
                context_chars=args.context_chars,
            )
        )

    def _handle_dev_courses_api(self, args: argparse.Namespace) -> Any:
        return self._run_klms_async(lambda: courses.list_courses_api(include_all=args.include_all, limit=args.limit))

    def _handle_dev_term(self, args: argparse.Namespace) -> Any:
        return self._run_klms_async(lambda: courses.get_current_term())

    def _handle_dev_course_info(self, args: argparse.Namespace) -> Any:
        return self._run_klms_async(lambda: courses.get_course(args.course_id))

    def _handle_dev_discover_api(self, args: argparse.Namespace) -> Any:
        return self._run_klms_async(
            lambda: legacy.klms_discover_api(
                max_courses=args.max_courses,
                max_notice_boards=args.max_notice_boards,
            )
        )

    def _handle_dev_map_api(self, args: argparse.Namespace) -> dict[str, Any]:
        return legacy.klms_map_api(report_path=args.report_path)
