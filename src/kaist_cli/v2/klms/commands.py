from __future__ import annotations

import argparse

from ..contracts import CommandResult
from .container import KlmsFacade


def dispatch(args: argparse.Namespace, facade: KlmsFacade) -> CommandResult:
    if args.group == "auth" and args.action == "login":
        return facade.auth_login(
            base_url=args.base_url,
            dashboard_path=args.dashboard_path,
            username=args.username,
            wait_seconds=args.wait_seconds,
        )
    if args.group == "auth" and args.action == "install-browser":
        return facade.auth_install_browser(force=args.force)
    if args.group == "auth" and args.action == "status":
        return facade.auth_status()
    if args.group == "auth" and args.action == "refresh":
        return facade.auth_refresh(
            base_url=args.base_url,
            dashboard_path=args.dashboard_path,
            username=args.username,
            wait_seconds=args.wait_seconds,
        )
    if args.group == "auth" and args.action == "doctor":
        return facade.auth_doctor()
    if args.group == "today":
        return facade.today(
            limit=args.limit,
            window_days=args.window_days,
            notice_days=args.notice_days,
            max_notice_pages=args.max_notice_pages,
        )
    if args.group == "inbox":
        return facade.inbox(limit=args.limit, max_notice_pages=args.max_notice_pages, since_iso=args.since_iso)
    if args.group == "sync" and args.action == "run":
        return facade.sync_run()
    if args.group == "sync" and args.action == "status":
        return facade.sync_status()
    if args.group == "sync" and args.action == "reset":
        return facade.sync_reset()
    if args.group == "courses" and args.action == "list":
        return facade.list_courses(
            include_all=args.include_all,
            include_past=args.include_past,
            limit=args.limit,
            course_query=args.course,
        )
    if args.group == "courses" and args.action == "show":
        return facade.show_course(args.course_id)
    if args.group == "assignments" and args.action == "list":
        return facade.list_assignments(
            course_id=args.course_id,
            course_query=args.course,
            since_iso=args.since_iso,
            limit=args.limit,
            include_past=args.include_past,
        )
    if args.group == "assignments" and args.action == "show":
        return facade.show_assignment(args.assignment_id, course_id_hint=args.course_id)
    if args.group == "notices" and args.action == "list":
        return facade.list_notices(
            notice_board_id=args.notice_board_id,
            course_id=args.course_id,
            course_query=args.course,
            max_pages=args.max_pages,
            since_iso=args.since_iso,
            limit=args.limit,
        )
    if args.group == "notices" and args.action == "show":
        return facade.show_notice(
            args.notice_id,
            notice_board_id=args.notice_board_id,
            course_id=args.course_id,
            course_query=args.course,
            max_pages=args.max_pages,
            include_html=args.include_html,
        )
    if args.group == "notices" and args.action == "attachments" and args.attachments_action == "pull":
        return facade.pull_notice_attachments(
            course_id=args.course_id,
            course_query=args.course,
            since_iso=args.since_iso,
            limit=args.limit,
            subdir=args.subdir,
            dest=args.dest,
            if_exists=args.if_exists,
        )
    if args.group == "files" and args.action == "list":
        return facade.list_files(course_id=args.course_id, course_query=args.course, limit=args.limit)
    if args.group == "files" and args.action == "get":
        return facade.get_file(args.file_id)
    if args.group == "files" and args.action == "download":
        return facade.download_file(
            args.file_id,
            filename=args.filename,
            subdir=args.subdir,
            dest=args.dest,
            if_exists=args.if_exists,
        )
    if args.group == "files" and args.action == "pull":
        return facade.pull_files(
            course_id=args.course_id,
            course_query=args.course,
            limit=args.limit,
            subdir=args.subdir,
            dest=args.dest,
            if_exists=args.if_exists,
        )
    if args.group == "videos" and args.action == "list":
        return facade.list_videos(course_id=args.course_id, course_query=args.course, limit=args.limit, recent=args.recent)
    if args.group == "videos" and args.action == "show":
        return facade.show_video(args.video_id, course_id_hint=args.course_id)
    if args.group == "dev" and args.action == "plan":
        return facade.dev_plan()
    if args.group == "dev" and args.action == "probe":
        return facade.dev_probe(live=args.live, timeout_seconds=args.timeout)
    if args.group == "dev" and args.action == "discover":
        return facade.dev_discover(
            max_courses=args.courses,
            max_notice_boards=args.boards,
            per_surface_links=args.links,
            manual_courseboard_seconds=args.manual_courseboard_seconds,
        )

    return facade.not_implemented(args.command_path)
