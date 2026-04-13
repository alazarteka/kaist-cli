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

    auth = klms_sub.add_parser(
        "auth",
        help="Log in, refresh, inspect, and troubleshoot KLMS authentication.",
        description="Log in, refresh, inspect, and troubleshoot KLMS authentication.",
        formatter_class=_HelpFormatter,
    )
    auth_sub = auth.add_subparsers(dest="action", required=True, metavar="ACTION")

    auth_login = auth_sub.add_parser(
        "login",
        help="Create a new KLMS session via interactive browser login or non-interactive KAIST Easy Login push approval.",
        description="Create a new KLMS session via interactive browser login or non-interactive KAIST Easy Login push approval.",
        formatter_class=_HelpFormatter,
    )
    auth_login.add_argument("--base-url", metavar="URL", help="Persist this KLMS base URL before opening the browser.")
    auth_login.add_argument("--dashboard-path", metavar="PATH", help="Optional dashboard path override, default /my/.")
    auth_login.add_argument("--username", metavar="ID", help="Use the non-interactive KAIST SSO Easy Login push flow with this KAIST account ID instead of the manual browser flow.")
    auth_login.add_argument("--wait-seconds", type=float, default=180.0, metavar="N", help="Maximum time to wait for KAIST Easy Login approval.")
    _set_defaults(auth_login, schema_name=f"{schema_prefix}.auth.login.v1", command_path="klms auth login", handler=handler)

    auth_install = auth_sub.add_parser(
        "install-browser",
        help="Install Playwright Chromium required by browser-backed KLMS auth flows.",
        description="Install Playwright Chromium required by browser-backed KLMS auth flows.",
        formatter_class=_HelpFormatter,
    )
    auth_install.add_argument("--force", action="store_true", help="Reinstall Chromium even if already installed.")
    _set_defaults(
        auth_install,
        schema_name=f"{schema_prefix}.auth.install_browser.v1",
        command_path="klms auth install-browser",
        handler=handler,
    )

    auth_status = auth_sub.add_parser(
        "status",
        help="Show saved auth artifacts, config, and the active auth mode.",
        description="Show saved auth artifacts, config, and the active auth mode.",
        formatter_class=_HelpFormatter,
    )
    _set_defaults(auth_status, schema_name=f"{schema_prefix}.auth.status.v1", command_path="klms auth status", handler=handler)

    auth_refresh = auth_sub.add_parser(
        "refresh",
        help="Renew an existing KLMS session using saved config and the saved non-interactive Easy Login username when available.",
        description="Renew an existing KLMS session using saved config and the saved non-interactive Easy Login username when available.",
        formatter_class=_HelpFormatter,
    )
    auth_refresh.add_argument("--base-url", metavar="URL", help="Optional base URL override before refreshing auth.")
    auth_refresh.add_argument("--dashboard-path", metavar="PATH", help="Optional dashboard path override, default /my/.")
    auth_refresh.add_argument("--username", metavar="ID", help="Use the non-interactive KAIST SSO Easy Login push flow with this KAIST account ID instead of the manual browser flow.")
    auth_refresh.add_argument("--wait-seconds", type=float, default=180.0, metavar="N", help="Maximum time to wait for KAIST Easy Login approval.")
    _set_defaults(
        auth_refresh,
        schema_name=f"{schema_prefix}.auth.refresh.v1",
        command_path="klms auth refresh",
        handler=handler,
    )

    auth_setup_email = auth_sub.add_parser(
        "setup-email-otp",
        help="Configure email-OTP KLMS auth and save non-secret config. Password storage is a separate explicit step by default.",
        description="Configure email-OTP KLMS auth and save non-secret config. Password storage is a separate explicit step by default.",
        formatter_class=_HelpFormatter,
    )
    auth_setup_email.add_argument("--base-url", metavar="URL", help="Persist this KLMS base URL before configuring email OTP auth.")
    auth_setup_email.add_argument("--dashboard-path", metavar="PATH", help="Optional dashboard path override, default /my/.")
    auth_setup_email.add_argument("--username", metavar="ID", required=True, help="KAIST account ID used for username/password login.")
    auth_setup_email.add_argument("--otp-source", metavar="SOURCE", default="manual", help="Human-readable OTP source label, for example `manual` or `gmail_connector`.")
    auth_setup_email.add_argument("--password-env", metavar="ENV", help="Optionally read the KAIST password from this environment variable and store it in macOS Keychain during setup.")
    auth_setup_email.add_argument("--prompt-password", action="store_true", help="Prompt for the KAIST password in the current terminal and store it in macOS Keychain during setup.")
    _set_defaults(
        auth_setup_email,
        schema_name=f"{schema_prefix}.auth.setup_email_otp.v1",
        command_path="klms auth setup-email-otp",
        handler=handler,
    )

    auth_store_email_secret = auth_sub.add_parser(
        "store-email-otp-secret",
        help="Store the KAIST password for email-OTP auth in macOS Keychain. Intended for a human-run separate terminal.",
        description="Store the KAIST password for email-OTP auth in macOS Keychain. Intended for a human-run separate terminal.",
        formatter_class=_HelpFormatter,
    )
    auth_store_email_secret.add_argument("--username", metavar="ID", help="KAIST account ID override. Defaults to the saved email-OTP auth username.")
    auth_store_email_secret.add_argument("--password-env", metavar="ENV", help="Read the KAIST password from this environment variable instead of prompting interactively.")
    _set_defaults(
        auth_store_email_secret,
        schema_name=f"{schema_prefix}.auth.store_email_otp_secret.v1",
        command_path="klms auth store-email-otp-secret",
        handler=handler,
    )

    auth_clear_email_secret = auth_sub.add_parser(
        "clear-email-otp-secret",
        help="Delete the stored email-OTP KAIST password from macOS Keychain.",
        description="Delete the stored email-OTP KAIST password from macOS Keychain.",
        formatter_class=_HelpFormatter,
    )
    auth_clear_email_secret.add_argument("--username", metavar="ID", help="KAIST account ID override. Defaults to the saved email-OTP auth username.")
    _set_defaults(
        auth_clear_email_secret,
        schema_name=f"{schema_prefix}.auth.clear_email_otp_secret.v1",
        command_path="klms auth clear-email-otp-secret",
        handler=handler,
    )

    auth_begin_refresh = auth_sub.add_parser(
        "begin-refresh",
        help="Start a staged email-OTP KLMS refresh and stop once the SSO challenge is waiting for an OTP code.",
        description="Start a staged email-OTP KLMS refresh and stop once the SSO challenge is waiting for an OTP code.",
        formatter_class=_HelpFormatter,
    )
    auth_begin_refresh.add_argument("--base-url", metavar="URL", help="Optional base URL override before starting the staged refresh.")
    auth_begin_refresh.add_argument("--dashboard-path", metavar="PATH", help="Optional dashboard path override, default /my/.")
    auth_begin_refresh.add_argument("--username", metavar="ID", help="Optional KAIST account ID override.")
    auth_begin_refresh.add_argument("--wait-seconds", type=float, default=180.0, metavar="N", help="Maximum time to wait for the email OTP challenge page.")
    _set_defaults(
        auth_begin_refresh,
        schema_name=f"{schema_prefix}.auth.begin_refresh.v1",
        command_path="klms auth begin-refresh",
        handler=handler,
    )

    auth_complete_refresh = auth_sub.add_parser(
        "complete-refresh",
        help="Submit an OTP code for a previously started staged email-OTP refresh.",
        description="Submit an OTP code for a previously started staged email-OTP refresh.",
        formatter_class=_HelpFormatter,
    )
    auth_complete_refresh.add_argument("session_id", metavar="SESSION_ID", help="Auth session ID returned by `begin-refresh`.")
    auth_complete_refresh.add_argument("--otp", metavar="CODE", required=True, help="OTP code received from KAIST email.")
    auth_complete_refresh.add_argument("--wait-seconds", type=float, default=180.0, metavar="N", help="Maximum time to wait for KLMS session completion after OTP submit.")
    _set_defaults(
        auth_complete_refresh,
        schema_name=f"{schema_prefix}.auth.complete_refresh.v1",
        command_path="klms auth complete-refresh",
        handler=handler,
    )

    auth_cancel_refresh = auth_sub.add_parser(
        "cancel-refresh",
        help="Cancel and clear a previously started staged email-OTP refresh session.",
        description="Cancel and clear a previously started staged email-OTP refresh session.",
        formatter_class=_HelpFormatter,
    )
    auth_cancel_refresh.add_argument("session_id", metavar="SESSION_ID", help="Auth session ID returned by `begin-refresh`.")
    _set_defaults(
        auth_cancel_refresh,
        schema_name=f"{schema_prefix}.auth.cancel_refresh.v1",
        command_path="klms auth cancel-refresh",
        handler=handler,
    )

    auth_worker_run = auth_sub.add_parser(
        "_worker-run",
        help=argparse.SUPPRESS,
        description=argparse.SUPPRESS,
        formatter_class=_HelpFormatter,
    )
    auth_worker_run.add_argument("session_id", metavar="SESSION_ID", help=argparse.SUPPRESS)
    _set_defaults(
        auth_worker_run,
        schema_name=f"{schema_prefix}.auth.worker_run.v1",
        command_path="klms auth _worker-run",
        handler=handler,
    )

    auth_doctor = auth_sub.add_parser(
        "doctor",
        help="Run offline checks against saved KLMS auth state and config.",
        description="Run offline checks against saved KLMS auth state and config.",
        formatter_class=_HelpFormatter,
    )
    _set_defaults(auth_doctor, schema_name=f"{schema_prefix}.auth.doctor.v1", command_path="klms auth doctor", handler=handler)

    today = klms_sub.add_parser(
        "today",
        help="Show an urgency-focused daily view of near-term assignments, recent notices, and useful materials.",
        description="Show an urgency-focused daily view of near-term assignments, recent notices, and useful materials.",
        formatter_class=_HelpFormatter,
    )
    today.add_argument("--limit", type=int, default=5, metavar="N", help="Maximum items per section.")
    today.add_argument("--window-days", type=int, default=7, metavar="N", help="Assignment due-soon window in days.")
    today.add_argument("--notice-days", type=int, default=3, metavar="N", help="Recent-notice window in days.")
    today.add_argument("--max-notice-pages", type=int, default=1, metavar="N", help="Maximum notice pages per board.")
    _set_defaults(today, schema_name=f"{schema_prefix}.today.v1", command_path="klms today", handler=handler)

    week = klms_sub.add_parser(
        "week",
        help="Show this week’s assignments, notices, and new materials in one weekly summary view.",
        description="Show this week’s assignments, notices, and new materials in one weekly summary view.",
        formatter_class=_HelpFormatter,
    )
    week.add_argument("--limit", type=int, default=8, metavar="N", help="Maximum items per section.")
    week.add_argument("--max-notice-pages", type=int, default=2, metavar="N", help="Maximum notice pages per board.")
    _set_defaults(week, schema_name=f"{schema_prefix}.week.v1", command_path="klms week", handler=handler)

    inbox = klms_sub.add_parser(
        "inbox",
        help="Show a broader chronological KLMS feed merged across assignments, notices, and materials.",
        description="Show a broader chronological KLMS feed merged across assignments, notices, and materials.",
        formatter_class=_HelpFormatter,
    )
    inbox.add_argument("--limit", type=int, default=30, metavar="N", help="Maximum number of inbox items to return.")
    inbox.add_argument("--max-notice-pages", type=int, default=1, metavar="N", help="Maximum notice pages per board.")
    inbox.add_argument("--since", dest="since_iso", metavar="ISO", help="Filter time-bearing items by ISO timestamp.")
    _set_defaults(inbox, schema_name=f"{schema_prefix}.inbox.v1", command_path="klms inbox", handler=handler)

    sync = klms_sub.add_parser(
        "sync",
        help="Refresh, inspect, and clear cached KLMS state.",
        description="Refresh, inspect, and clear cached KLMS state.",
        formatter_class=_HelpFormatter,
    )
    sync_sub = sync.add_subparsers(dest="action", required=True, metavar="ACTION")
    sync_run = sync_sub.add_parser(
        "run",
        help="Refresh cached notice and file data so later interactive commands are faster.",
        description="Refresh cached notice and file data so later interactive commands are faster.",
        formatter_class=_HelpFormatter,
    )
    _set_defaults(sync_run, schema_name=f"{schema_prefix}.sync.run.v1", command_path="klms sync run", handler=handler)
    sync_status = sync_sub.add_parser(
        "status",
        help="Show cache freshness, expiry, and provider state for KLMS sync data.",
        description="Show cache freshness, expiry, and provider state for KLMS sync data.",
        formatter_class=_HelpFormatter,
    )
    _set_defaults(sync_status, schema_name=f"{schema_prefix}.sync.status.v1", command_path="klms sync status", handler=handler)
    sync_reset = sync_sub.add_parser(
        "reset",
        help="Delete KLMS sync/cache state without touching saved auth artifacts.",
        description="Delete KLMS sync/cache state without touching saved auth artifacts.",
        formatter_class=_HelpFormatter,
    )
    _set_defaults(sync_reset, schema_name=f"{schema_prefix}.sync.reset.v1", command_path="klms sync reset", handler=handler)

    courses = klms_sub.add_parser("courses", help="List and inspect KLMS courses.", formatter_class=_HelpFormatter)
    courses_sub = courses.add_subparsers(dest="action", required=True, metavar="ACTION")
    courses_list = courses_sub.add_parser("list", help="List current-term KLMS courses from the dashboard.", formatter_class=_HelpFormatter)
    courses_list.add_argument("--include-all", action="store_true", help="Include noisy dashboard cards and non-course items that are hidden by default.")
    courses_list.add_argument("--include-past", action="store_true", help="Include past-term courses instead of defaulting to the current term.")
    courses_list.add_argument("--course", metavar="QUERY", help="Filter by course code or title substring.")
    courses_list.add_argument("--limit", type=int, metavar="N", help="Maximum number of courses to return.")
    _set_defaults(courses_list, schema_name=f"{schema_prefix}.courses.list.v1", command_path="klms courses list", handler=handler)
    courses_resolve = courses_sub.add_parser("resolve", help="Resolve a course query to concrete course IDs and matching aliases.", formatter_class=_HelpFormatter)
    courses_resolve.add_argument("query", metavar="QUERY", help="Course ID, code, Korean title, or English title.")
    courses_resolve.add_argument("--include-all", action="store_true", help="Include noisy dashboard cards and non-course items that are hidden by default.")
    courses_resolve.add_argument("--include-past", action="store_true", help="Include past-term courses instead of defaulting to the current term.")
    courses_resolve.add_argument("--limit", type=int, default=10, metavar="N", help="Maximum number of matching courses to return.")
    _set_defaults(courses_resolve, schema_name=f"{schema_prefix}.courses.resolve.v1", command_path="klms courses resolve", handler=handler)
    courses_show = courses_sub.add_parser("show", help="Show course metadata and details for one course ID.", formatter_class=_HelpFormatter)
    courses_show.add_argument("course_id", metavar="ID", help="Course ID.")
    _set_defaults(courses_show, schema_name=f"{schema_prefix}.courses.show.v1", command_path="klms courses show", handler=handler)

    assignments = klms_sub.add_parser("assignments", help="List and inspect KLMS assignments.", formatter_class=_HelpFormatter)
    assignments_sub = assignments.add_subparsers(dest="action", required=True, metavar="ACTION")
    assignments_list = assignments_sub.add_parser("list", help="List current-term assignments, optionally narrowed by course or due date.", formatter_class=_HelpFormatter)
    assignments_list.add_argument("--course-id", metavar="ID", help="Filter to one course ID.")
    assignments_list.add_argument("--course", metavar="QUERY", help="Filter by course code or title substring.")
    assignments_list.add_argument("--include-past", action="store_true", help="Include past-term assignments instead of defaulting to current-term courses.")
    assignments_list.add_argument("--since", dest="since_iso", metavar="ISO", help="Only include assignments with due_iso >= ISO.")
    assignments_list.add_argument("--limit", type=int, metavar="N", help="Maximum number of assignments to return.")
    _set_defaults(
        assignments_list,
        schema_name=f"{schema_prefix}.assignments.list.v1",
        command_path="klms assignments list",
        handler=handler,
    )
    assignments_show = assignments_sub.add_parser("show", help="Show the full detail page for one assignment ID.", formatter_class=_HelpFormatter)
    assignments_show.add_argument("assignment_id", metavar="ID", help="Assignment ID.")
    assignments_show.add_argument("--course-id", metavar="ID", help="Optional course scope hint.")
    _set_defaults(
        assignments_show,
        schema_name=f"{schema_prefix}.assignments.show.v1",
        command_path="klms assignments show",
        handler=handler,
    )

    notices = klms_sub.add_parser("notices", help="List and inspect courseboard notice posts.", formatter_class=_HelpFormatter)
    notices_sub = notices.add_subparsers(dest="action", required=True, metavar="ACTION")
    notices_list = notices_sub.add_parser("list", help="List recent notices from current-term course boards.", formatter_class=_HelpFormatter)
    notices_list.add_argument("--notice-board-id", metavar="ID", help="Filter to one notice board ID.")
    notices_list.add_argument("--course-id", metavar="ID", help="Filter to boards belonging to one course ID.")
    notices_list.add_argument("--course", metavar="QUERY", help="Filter to boards belonging to matching course code or title text.")
    notices_list.add_argument("--max-pages", type=int, default=1, metavar="N", help="Maximum pages per board.")
    notices_list.add_argument("--since", dest="since_iso", metavar="ISO", help="Only include notices with posted_iso >= ISO.")
    notices_list.add_argument("--limit", type=int, metavar="N", help="Maximum number of notices to return.")
    _set_defaults(
        notices_list,
        schema_name=f"{schema_prefix}.notices.list.v1",
        command_path="klms notices list",
        handler=handler,
    )
    notices_show = notices_sub.add_parser("show", help="Show one notice article and resolve its board automatically when possible.", formatter_class=_HelpFormatter)
    notices_show.add_argument("notice_id", metavar="ID", help="Notice ID.")
    notices_show.add_argument("--notice-board-id", metavar="ID", help="Optional board scope that speeds lookup if you already know the board.")
    notices_show.add_argument("--course-id", metavar="ID", help="Optional course ID scope used while discovering the notice board.")
    notices_show.add_argument("--course", metavar="QUERY", help="Optional course code/title filter used while discovering the notice board.")
    notices_show.add_argument("--max-pages", type=int, default=3, metavar="N", help="Maximum pages per board to scan.")
    notices_show.add_argument("--include-html", action="store_true", help="Include parsed notice body HTML in output.")
    _set_defaults(
        notices_show,
        schema_name=f"{schema_prefix}.notices.show.v1",
        command_path="klms notices show",
        handler=handler,
    )
    notices_attachments = notices_sub.add_parser("attachments", help="Operate on notice-post attachments.", formatter_class=_HelpFormatter)
    notices_attachments_sub = notices_attachments.add_subparsers(dest="attachments_action", required=True, metavar="ACTION")
    notices_attachments_pull = notices_attachments_sub.add_parser(
        "pull",
        help="Download notice-post attachments into a local mirror using authenticated HTTP where possible.",
        formatter_class=_HelpFormatter,
    )
    notices_attachments_pull.add_argument("--course-id", metavar="ID", help="Filter to boards belonging to one course ID.")
    notices_attachments_pull.add_argument("--course", metavar="QUERY", help="Filter to boards belonging to matching course code or title text.")
    notices_attachments_pull.add_argument("--since", dest="since_iso", metavar="ISO", help="Only scan notices with posted_iso >= ISO.")
    notices_attachments_pull.add_argument("--limit", type=int, metavar="N", help="Maximum number of notices to scan for attachments.")
    notices_attachments_pull.add_argument("--subdir", metavar="DIR", help="Relative destination under the v2 files root.")
    notices_attachments_pull.add_argument("--dest", metavar="PATH", help="Write into this directory instead of the managed files root.")
    notices_attachments_pull.add_argument("--if-exists", choices=["skip", "overwrite"], default="skip", help="Behavior when the destination exists.")
    _set_defaults(
        notices_attachments_pull,
        schema_name=f"{schema_prefix}.notices.attachments.pull.v1",
        command_path="klms notices attachments pull",
        handler=handler,
    )

    files = klms_sub.add_parser("files", help="List, resolve, download, and mirror KLMS files and materials.", formatter_class=_HelpFormatter)
    files_sub = files.add_subparsers(dest="action", required=True, metavar="ACTION")
    files_list = files_sub.add_parser("list", help="List current-term file and material entries from KLMS course surfaces.", formatter_class=_HelpFormatter)
    files_list.add_argument("--course-id", metavar="ID", help="Filter to one course ID.")
    files_list.add_argument("--course", metavar="QUERY", help="Filter by course code or title substring.")
    files_list.add_argument("--limit", type=int, metavar="N", help="Maximum number of file/material entries to return.")
    _set_defaults(files_list, schema_name=f"{schema_prefix}.files.list.v1", command_path="klms files list", handler=handler)
    files_get = files_sub.add_parser("get", help="Resolve one file/material to metadata without downloading content.", formatter_class=_HelpFormatter)
    files_get.add_argument("file_id", metavar="ID", help="File ID or URL.")
    _set_defaults(files_get, schema_name=f"{schema_prefix}.files.get.v1", command_path="klms files get", handler=handler)
    files_download = files_sub.add_parser("download", help="Download one directly downloadable file/material.", formatter_class=_HelpFormatter)
    files_download.add_argument("file_id", metavar="ID", help="File ID or URL.")
    files_download.add_argument("--filename", metavar="NAME", help="Optional output filename override.")
    files_download.add_argument("--subdir", metavar="DIR", help="Relative destination under the v2 files root.")
    files_download.add_argument("--dest", metavar="PATH", help="Write into this directory instead of the managed files root.")
    files_download.add_argument("--if-exists", choices=["skip", "overwrite"], default="skip", help="Behavior when the destination exists.")
    _set_defaults(
        files_download,
        schema_name=f"{schema_prefix}.files.download.v1",
        command_path="klms files download",
        handler=handler,
    )
    files_pull = files_sub.add_parser("pull", help="Bulk-download downloadable file/material entries into a local mirror.", formatter_class=_HelpFormatter)
    files_pull.add_argument("--course-id", metavar="ID", help="Filter to one course ID.")
    files_pull.add_argument("--course", metavar="QUERY", help="Filter by course code or title substring.")
    files_pull.add_argument("--limit", type=int, metavar="N", help="Maximum number of downloadable items to pull.")
    files_pull.add_argument("--subdir", metavar="DIR", help="Relative destination under the v2 files root.")
    files_pull.add_argument("--dest", metavar="PATH", help="Write into this directory instead of the managed files root.")
    files_pull.add_argument("--if-exists", choices=["skip", "overwrite"], default="skip", help="Behavior when the destination exists.")
    _set_defaults(files_pull, schema_name=f"{schema_prefix}.files.pull.v1", command_path="klms files pull", handler=handler)

    videos = klms_sub.add_parser("videos", help="List and inspect KLMS VOD lecture videos.", formatter_class=_HelpFormatter)
    videos_sub = videos.add_subparsers(dest="action", required=True, metavar="ACTION")
    videos_list = videos_sub.add_parser("list", help="List current-term course videos in discovered course/section order.", formatter_class=_HelpFormatter)
    videos_list.add_argument("--course-id", metavar="ID", help="Filter to one course ID.")
    videos_list.add_argument("--course", metavar="QUERY", help="Filter by course code or title substring.")
    videos_list.add_argument("--recent", action="store_true", help="Prefer recently surfaced videos instead of course-page order.")
    videos_list.add_argument("--limit", type=int, metavar="N", help="Maximum number of videos to return.")
    _set_defaults(videos_list, schema_name=f"{schema_prefix}.videos.list.v1", command_path="klms videos list", handler=handler)
    videos_show = videos_sub.add_parser("show", help="Show one video detail page, viewer link, and resolved stream URL when available.", formatter_class=_HelpFormatter)
    videos_show.add_argument("video_id", metavar="ID", help="Video ID or URL.")
    videos_show.add_argument("--course-id", metavar="ID", help="Optional course scope hint.")
    _set_defaults(videos_show, schema_name=f"{schema_prefix}.videos.show.v1", command_path="klms videos show", handler=handler)

    request = klms_sub.add_parser("request", help="Authenticated low-level read-only request helpers for KLMS repair/debugging.", formatter_class=_HelpFormatter)
    request_sub = request.add_subparsers(dest="action", required=True, metavar="ACTION")
    request_get = request_sub.add_parser("get", help="Run an authenticated GET against a KLMS path or URL and return stable JSON.", formatter_class=_HelpFormatter)
    request_get.add_argument("target", metavar="TARGET", help="KLMS path like `/course/view.php?id=12345` or a full URL under the configured base URL.")
    request_get.add_argument("--preview-chars", type=int, default=4000, metavar="N", help="Maximum body characters to include before truncating.")
    request_get.add_argument("--full-body", action="store_true", help="Include the full response body instead of truncating to a preview.")
    _set_defaults(request_get, schema_name=f"{schema_prefix}.request.get.v1", command_path="klms request get", handler=handler)

    dev = klms_sub.add_parser("dev", help="Engineering, probe, and discovery commands for KLMS internals.", formatter_class=_HelpFormatter)
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
