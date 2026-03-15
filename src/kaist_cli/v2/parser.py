from __future__ import annotations

import argparse
from typing import Any, Callable


class _HelpFormatter(argparse.ArgumentDefaultsHelpFormatter, argparse.RawDescriptionHelpFormatter):
    pass


Handler = Callable[[argparse.Namespace], Any]


def _set_defaults(
    parser: argparse.ArgumentParser,
    *,
    schema_name: str,
    command_path: str,
    handler: Handler | None,
) -> None:
    defaults: dict[str, Any] = {
        "schema_name": schema_name,
        "command_path": command_path,
    }
    if handler is not None:
        defaults["handler"] = handler
    parser.set_defaults(**defaults)


def register_klms_parser(
    klms: argparse.ArgumentParser,
    *,
    schema_prefix: str = "kaist.klms",
    handler: Handler | None = None,
) -> None:
    klms_sub = klms.add_subparsers(dest="group", required=True, metavar="COMMAND")

    auth = klms_sub.add_parser("auth", help="Authentication lifecycle", formatter_class=_HelpFormatter)
    auth_sub = auth.add_subparsers(dest="action", required=True, metavar="ACTION")

    auth_login = auth_sub.add_parser("login", help="Interactive login bootstrap", formatter_class=_HelpFormatter)
    auth_login.add_argument("--base-url", metavar="URL", help="Persist this KLMS base URL before opening the browser.")
    auth_login.add_argument("--dashboard-path", metavar="PATH", help="Optional dashboard path override, default /my/.")
    _set_defaults(auth_login, schema_name=f"{schema_prefix}.auth.login.v1", command_path="klms auth login", handler=handler)

    auth_install = auth_sub.add_parser(
        "install-browser",
        help="Install Playwright Chromium for KLMS auth commands",
        formatter_class=_HelpFormatter,
    )
    auth_install.add_argument("--force", action="store_true", help="Reinstall Chromium even if already installed.")
    _set_defaults(
        auth_install,
        schema_name=f"{schema_prefix}.auth.install_browser.v1",
        command_path="klms auth install-browser",
        handler=handler,
    )

    auth_status = auth_sub.add_parser("status", help="Show current auth strategy", formatter_class=_HelpFormatter)
    _set_defaults(auth_status, schema_name=f"{schema_prefix}.auth.status.v1", command_path="klms auth status", handler=handler)

    auth_refresh = auth_sub.add_parser("refresh", help="Refresh existing auth", formatter_class=_HelpFormatter)
    auth_refresh.add_argument("--base-url", metavar="URL", help="Optional base URL override before refreshing auth.")
    auth_refresh.add_argument("--dashboard-path", metavar="PATH", help="Optional dashboard path override, default /my/.")
    _set_defaults(
        auth_refresh,
        schema_name=f"{schema_prefix}.auth.refresh.v1",
        command_path="klms auth refresh",
        handler=handler,
    )

    auth_doctor = auth_sub.add_parser("doctor", help="Diagnose auth state", formatter_class=_HelpFormatter)
    _set_defaults(auth_doctor, schema_name=f"{schema_prefix}.auth.doctor.v1", command_path="klms auth doctor", handler=handler)

    today = klms_sub.add_parser("today", help="Show the student-facing daily view", formatter_class=_HelpFormatter)
    today.add_argument("--limit", type=int, default=5, metavar="N", help="Maximum items per section.")
    today.add_argument("--window-days", type=int, default=7, metavar="N", help="Assignment due-soon window in days.")
    today.add_argument("--notice-days", type=int, default=3, metavar="N", help="Recent-notice window in days.")
    today.add_argument("--max-notice-pages", type=int, default=1, metavar="N", help="Maximum notice pages per board.")
    _set_defaults(today, schema_name=f"{schema_prefix}.today.v1", command_path="klms today", handler=handler)

    inbox = klms_sub.add_parser("inbox", help="Show the blended inbox view", formatter_class=_HelpFormatter)
    inbox.add_argument("--limit", type=int, default=30, metavar="N", help="Maximum number of inbox items to return.")
    inbox.add_argument("--max-notice-pages", type=int, default=1, metavar="N", help="Maximum notice pages per board.")
    inbox.add_argument("--since", dest="since_iso", metavar="ISO", help="Filter time-bearing items by ISO timestamp.")
    _set_defaults(inbox, schema_name=f"{schema_prefix}.inbox.v1", command_path="klms inbox", handler=handler)

    sync = klms_sub.add_parser("sync", help="Local state sync", formatter_class=_HelpFormatter)
    sync_sub = sync.add_subparsers(dest="action", required=True, metavar="ACTION")
    sync_run = sync_sub.add_parser("run", help="Update local state", formatter_class=_HelpFormatter)
    _set_defaults(sync_run, schema_name=f"{schema_prefix}.sync.run.v1", command_path="klms sync run", handler=handler)
    sync_status = sync_sub.add_parser("status", help="Show sync status", formatter_class=_HelpFormatter)
    _set_defaults(sync_status, schema_name=f"{schema_prefix}.sync.status.v1", command_path="klms sync status", handler=handler)
    sync_reset = sync_sub.add_parser("reset", help="Reset local sync state", formatter_class=_HelpFormatter)
    _set_defaults(sync_reset, schema_name=f"{schema_prefix}.sync.reset.v1", command_path="klms sync reset", handler=handler)

    courses = klms_sub.add_parser("courses", help="Course resources", formatter_class=_HelpFormatter)
    courses_sub = courses.add_subparsers(dest="action", required=True, metavar="ACTION")
    courses_list = courses_sub.add_parser("list", help="List courses", formatter_class=_HelpFormatter)
    courses_list.add_argument("--include-all", action="store_true", help="Include noisy/non-course dashboard items.")
    courses_list.add_argument("--limit", type=int, metavar="N", help="Maximum number of courses to return.")
    _set_defaults(courses_list, schema_name=f"{schema_prefix}.courses.list.v1", command_path="klms courses list", handler=handler)
    courses_show = courses_sub.add_parser("show", help="Show one course", formatter_class=_HelpFormatter)
    courses_show.add_argument("course_id", metavar="ID", help="Course ID.")
    _set_defaults(courses_show, schema_name=f"{schema_prefix}.courses.show.v1", command_path="klms courses show", handler=handler)

    assignments = klms_sub.add_parser("assignments", help="Assignment resources", formatter_class=_HelpFormatter)
    assignments_sub = assignments.add_subparsers(dest="action", required=True, metavar="ACTION")
    assignments_list = assignments_sub.add_parser("list", help="List assignments", formatter_class=_HelpFormatter)
    assignments_list.add_argument("--course-id", metavar="ID", help="Filter to one course ID.")
    assignments_list.add_argument("--since", dest="since_iso", metavar="ISO", help="Only include assignments with due_iso >= ISO.")
    assignments_list.add_argument("--limit", type=int, metavar="N", help="Maximum number of assignments to return.")
    _set_defaults(
        assignments_list,
        schema_name=f"{schema_prefix}.assignments.list.v1",
        command_path="klms assignments list",
        handler=handler,
    )
    assignments_show = assignments_sub.add_parser("show", help="Show one assignment", formatter_class=_HelpFormatter)
    assignments_show.add_argument("assignment_id", metavar="ID", help="Assignment ID.")
    assignments_show.add_argument("--course-id", metavar="ID", help="Optional course scope hint.")
    _set_defaults(
        assignments_show,
        schema_name=f"{schema_prefix}.assignments.show.v1",
        command_path="klms assignments show",
        handler=handler,
    )

    notices = klms_sub.add_parser("notices", help="Notice resources", formatter_class=_HelpFormatter)
    notices_sub = notices.add_subparsers(dest="action", required=True, metavar="ACTION")
    notices_list = notices_sub.add_parser("list", help="List notices", formatter_class=_HelpFormatter)
    notices_list.add_argument("--notice-board-id", metavar="ID", help="Filter to one notice board ID.")
    notices_list.add_argument("--max-pages", type=int, default=1, metavar="N", help="Maximum pages per board.")
    notices_list.add_argument("--since", dest="since_iso", metavar="ISO", help="Only include notices with posted_iso >= ISO.")
    notices_list.add_argument("--limit", type=int, metavar="N", help="Maximum number of notices to return.")
    _set_defaults(
        notices_list,
        schema_name=f"{schema_prefix}.notices.list.v1",
        command_path="klms notices list",
        handler=handler,
    )
    notices_show = notices_sub.add_parser("show", help="Show one notice", formatter_class=_HelpFormatter)
    notices_show.add_argument("notice_id", metavar="ID", help="Notice ID.")
    notices_show.add_argument("--notice-board-id", metavar="ID", help="Optional board scope.")
    notices_show.add_argument("--max-pages", type=int, default=3, metavar="N", help="Maximum pages per board to scan.")
    notices_show.add_argument("--include-html", action="store_true", help="Include parsed notice body HTML in output.")
    _set_defaults(
        notices_show,
        schema_name=f"{schema_prefix}.notices.show.v1",
        command_path="klms notices show",
        handler=handler,
    )

    files = klms_sub.add_parser("files", help="File resources", formatter_class=_HelpFormatter)
    files_sub = files.add_subparsers(dest="action", required=True, metavar="ACTION")
    files_list = files_sub.add_parser("list", help="List files", formatter_class=_HelpFormatter)
    files_list.add_argument("--course-id", metavar="ID", help="Filter to one course ID.")
    files_list.add_argument("--limit", type=int, metavar="N", help="Maximum number of file/material entries to return.")
    _set_defaults(files_list, schema_name=f"{schema_prefix}.files.list.v1", command_path="klms files list", handler=handler)
    files_get = files_sub.add_parser("get", help="Resolve one file", formatter_class=_HelpFormatter)
    files_get.add_argument("file_id", metavar="ID", help="File ID or URL.")
    _set_defaults(files_get, schema_name=f"{schema_prefix}.files.get.v1", command_path="klms files get", handler=handler)
    files_download = files_sub.add_parser("download", help="Download one file", formatter_class=_HelpFormatter)
    files_download.add_argument("file_id", metavar="ID", help="File ID or URL.")
    files_download.add_argument("--filename", metavar="NAME", help="Optional output filename override.")
    files_download.add_argument("--subdir", metavar="DIR", help="Relative destination under the v2 files root.")
    files_download.add_argument("--if-exists", choices=["skip", "overwrite"], default="skip", help="Behavior when the destination exists.")
    _set_defaults(
        files_download,
        schema_name=f"{schema_prefix}.files.download.v1",
        command_path="klms files download",
        handler=handler,
    )
    files_pull = files_sub.add_parser("pull", help="Bulk-download downloadable file materials", formatter_class=_HelpFormatter)
    files_pull.add_argument("--course-id", metavar="ID", help="Filter to one course ID.")
    files_pull.add_argument("--limit", type=int, metavar="N", help="Maximum number of downloadable items to pull.")
    files_pull.add_argument("--subdir", metavar="DIR", help="Relative destination under the v2 files root.")
    files_pull.add_argument("--if-exists", choices=["skip", "overwrite"], default="skip", help="Behavior when the destination exists.")
    _set_defaults(files_pull, schema_name=f"{schema_prefix}.files.pull.v1", command_path="klms files pull", handler=handler)

    videos = klms_sub.add_parser("videos", help="Lecture videos / VOD", formatter_class=_HelpFormatter)
    videos_sub = videos.add_subparsers(dest="action", required=True, metavar="ACTION")
    videos_list = videos_sub.add_parser("list", help="List course videos", formatter_class=_HelpFormatter)
    videos_list.add_argument("--course-id", metavar="ID", help="Filter to one course ID.")
    videos_list.add_argument("--limit", type=int, metavar="N", help="Maximum number of videos to return.")
    _set_defaults(videos_list, schema_name=f"{schema_prefix}.videos.list.v1", command_path="klms videos list", handler=handler)
    videos_show = videos_sub.add_parser("show", help="Show one video", formatter_class=_HelpFormatter)
    videos_show.add_argument("video_id", metavar="ID", help="Video ID or URL.")
    videos_show.add_argument("--course-id", metavar="ID", help="Optional course scope hint.")
    _set_defaults(videos_show, schema_name=f"{schema_prefix}.videos.show.v1", command_path="klms videos show", handler=handler)

    dev = klms_sub.add_parser("dev", help="Engineering and discovery commands", formatter_class=_HelpFormatter)
    dev_sub = dev.add_subparsers(dest="action", required=True, metavar="ACTION")
    dev_plan = dev_sub.add_parser("plan", help="Show rewrite plan", formatter_class=_HelpFormatter)
    _set_defaults(dev_plan, schema_name=f"{schema_prefix}.dev.plan.v1", command_path="klms dev plan", handler=handler)
    dev_probe = dev_sub.add_parser("probe", help="Show provider probing strategy", formatter_class=_HelpFormatter)
    dev_probe.add_argument("--live", action="store_true", help="Perform live endpoint validation using current auth state.")
    dev_probe.add_argument("--timeout", type=float, default=10.0, metavar="SECONDS", help="Per-request timeout for live validation.")
    _set_defaults(dev_probe, schema_name=f"{schema_prefix}.dev.probe.v1", command_path="klms dev probe", handler=handler)
    dev_discover = dev_sub.add_parser("discover", help="Capture KLMS XHR/fetch endpoints from key surfaces", formatter_class=_HelpFormatter)
    dev_discover.add_argument("--courses", type=int, default=2, metavar="N", help="Maximum number of course dashboards to traverse.")
    dev_discover.add_argument("--boards", type=int, default=2, metavar="N", help="Maximum number of notice boards to traverse.")
    dev_discover.add_argument("--links", type=int, default=2, metavar="N", help="Maximum number of detail links to follow per surface type.")
    dev_discover.add_argument(
        "--manual-courseboard-seconds",
        type=int,
        default=0,
        metavar="N",
        help="Open the first discovered notice board in a visible browser and record runtime courseboard activity for N seconds while you click around.",
    )
    _set_defaults(
        dev_discover,
        schema_name=f"{schema_prefix}.dev.discover.v1",
        command_path="klms dev discover",
        handler=handler,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kaist",
        description="Task-first KLMS interface for the clean-break rewrite.",
        formatter_class=_HelpFormatter,
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON envelopes.")

    systems = parser.add_subparsers(dest="system", required=True, metavar="SYSTEM")

    klms = systems.add_parser(
        "klms",
        help="KAIST Learning Management System",
        description="Task-first KLMS interface.",
        formatter_class=_HelpFormatter,
    )
    register_klms_parser(klms)

    return parser
