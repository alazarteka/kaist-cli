from __future__ import annotations

from ..contracts import CommandError, CommandResult
from .auth import AuthService
from .assignments import AssignmentService
from .capture import EndpointCaptureService
from .courses import CourseService
from .dashboard import DashboardService
from .files import FileService
from .notices import NoticeService
from .paths import resolve_paths
from .probe import CapabilityProbeService
from .sync import SyncService
from .videos import VideoService


class KlmsFacade:
    def __init__(
        self,
        *,
        auth: AuthService,
        assignments: AssignmentService,
        notices: NoticeService,
        files: FileService,
        dashboard: DashboardService,
        sync: SyncService,
        probe: CapabilityProbeService,
        capture: EndpointCaptureService,
        courses: CourseService,
        videos: VideoService,
    ) -> None:
        self._auth = auth
        self._assignments = assignments
        self._notices = notices
        self._files = files
        self._dashboard = dashboard
        self._sync = sync
        self._probe = probe
        self._capture = capture
        self._courses = courses
        self._videos = videos

    def auth_login(
        self,
        *,
        base_url: str | None = None,
        dashboard_path: str | None = None,
        username: str | None = None,
        wait_seconds: float = 180.0,
    ) -> CommandResult:
        return self._auth.login(
            base_url=base_url,
            dashboard_path=dashboard_path,
            username=username,
            wait_seconds=wait_seconds,
        )

    def auth_install_browser(self, *, force: bool = False) -> CommandResult:
        return self._auth.install_browser(force=force)

    def auth_setup_email_otp(
        self,
        *,
        base_url: str | None = None,
        dashboard_path: str | None = None,
        username: str,
        otp_source: str = "manual",
        password_env: str | None = None,
        prompt_password: bool = False,
    ) -> CommandResult:
        return self._auth.setup_email_otp(
            base_url=base_url,
            dashboard_path=dashboard_path,
            username=username,
            otp_source=otp_source,
            password_env=password_env,
            prompt_password=prompt_password,
        )

    def auth_store_email_otp_secret(
        self,
        *,
        username: str | None = None,
        password_env: str | None = None,
    ) -> CommandResult:
        return self._auth.store_email_otp_secret(username=username, password_env=password_env)

    def auth_clear_email_otp_secret(self, *, username: str | None = None) -> CommandResult:
        return self._auth.clear_email_otp_secret(username=username)

    def auth_status(self) -> CommandResult:
        return self._auth.status()

    def auth_refresh(
        self,
        *,
        base_url: str | None = None,
        dashboard_path: str | None = None,
        username: str | None = None,
        wait_seconds: float = 180.0,
    ) -> CommandResult:
        return self._auth.refresh(
            base_url=base_url,
            dashboard_path=dashboard_path,
            username=username,
            wait_seconds=wait_seconds,
        )

    def auth_begin_refresh(
        self,
        *,
        base_url: str | None = None,
        dashboard_path: str | None = None,
        username: str | None = None,
        wait_seconds: float = 180.0,
    ) -> CommandResult:
        return self._auth.begin_refresh(
            base_url=base_url,
            dashboard_path=dashboard_path,
            username=username,
            wait_seconds=wait_seconds,
        )

    def auth_complete_refresh(
        self,
        session_id: str,
        *,
        otp: str,
        wait_seconds: float = 180.0,
    ) -> CommandResult:
        return self._auth.complete_refresh(session_id, otp=otp, wait_seconds=wait_seconds)

    def auth_cancel_refresh(self, session_id: str) -> CommandResult:
        return self._auth.cancel_refresh(session_id)

    def auth_worker_run(self, session_id: str) -> CommandResult:
        return self._auth.worker_run(session_id)

    def auth_doctor(self) -> CommandResult:
        return self._auth.doctor()

    def today(
        self,
        *,
        limit: int = 5,
        window_days: int = 7,
        notice_days: int = 3,
        max_notice_pages: int = 1,
    ) -> CommandResult:
        return self._dashboard.today(
            limit=limit,
            window_days=window_days,
            notice_days=notice_days,
            max_notice_pages=max_notice_pages,
        )

    def inbox(
        self,
        *,
        limit: int = 30,
        max_notice_pages: int = 1,
        since_iso: str | None = None,
    ) -> CommandResult:
        return self._dashboard.inbox(limit=limit, max_notice_pages=max_notice_pages, since_iso=since_iso)

    def sync_run(self) -> CommandResult:
        return self._sync.run()

    def sync_status(self) -> CommandResult:
        return self._sync.status()

    def sync_reset(self) -> CommandResult:
        return self._sync.reset()

    def dev_plan(self) -> CommandResult:
        return self._probe.plan()

    def dev_probe(self, *, live: bool = False, timeout_seconds: float = 10.0) -> CommandResult:
        return self._probe.probe(live=live, timeout_seconds=timeout_seconds)

    def dev_discover(
        self,
        *,
        max_courses: int = 2,
        max_notice_boards: int = 2,
        per_surface_links: int = 2,
        manual_courseboard_seconds: int = 0,
    ) -> CommandResult:
        return self._capture.discover(
            max_courses=max_courses,
            max_notice_boards=max_notice_boards,
            per_surface_links=per_surface_links,
            manual_courseboard_seconds=manual_courseboard_seconds,
        )

    def list_courses(
        self,
        *,
        include_all: bool = False,
        include_past: bool = False,
        limit: int | None = None,
        course_query: str | None = None,
    ) -> CommandResult:
        return self._courses.list(include_all=include_all, include_past=include_past, limit=limit, course_query=course_query)

    def show_course(self, course_id: str) -> CommandResult:
        return self._courses.show(course_id)

    def list_assignments(
        self,
        *,
        course_id: str | None = None,
        course_query: str | None = None,
        since_iso: str | None = None,
        limit: int | None = None,
        include_past: bool = False,
    ) -> CommandResult:
        return self._assignments.list(
            course_id=course_id,
            course_query=course_query,
            since_iso=since_iso,
            limit=limit,
            include_past=include_past,
        )

    def show_assignment(self, assignment_id: str, *, course_id_hint: str | None = None) -> CommandResult:
        return self._assignments.show(assignment_id, course_id_hint=course_id_hint)

    def list_notices(
        self,
        *,
        notice_board_id: str | None = None,
        course_id: str | None = None,
        course_query: str | None = None,
        max_pages: int = 1,
        since_iso: str | None = None,
        limit: int | None = None,
    ) -> CommandResult:
        return self._notices.list(
            notice_board_id=notice_board_id,
            course_id=course_id,
            course_query=course_query,
            max_pages=max_pages,
            since_iso=since_iso,
            limit=limit,
        )

    def show_notice(
        self,
        notice_id: str,
        *,
        notice_board_id: str | None = None,
        course_id: str | None = None,
        course_query: str | None = None,
        max_pages: int = 3,
        include_html: bool = False,
    ) -> CommandResult:
        return self._notices.show(
            notice_id,
            notice_board_id=notice_board_id,
            course_id=course_id,
            course_query=course_query,
            max_pages=max_pages,
            include_html=include_html,
        )

    def pull_notice_attachments(
        self,
        *,
        course_id: str | None = None,
        course_query: str | None = None,
        since_iso: str | None = None,
        limit: int | None = None,
        subdir: str | None = None,
        dest: str | None = None,
        if_exists: str = "skip",
    ) -> CommandResult:
        return self._notices.pull_attachments(
            course_id=course_id,
            course_query=course_query,
            since_iso=since_iso,
            limit=limit,
            subdir=subdir,
            dest=dest,
            if_exists=if_exists,
        )

    def list_files(self, *, course_id: str | None = None, course_query: str | None = None, limit: int | None = None) -> CommandResult:
        return self._files.list(course_id=course_id, course_query=course_query, limit=limit)

    def get_file(self, file_id_or_url: str) -> CommandResult:
        return self._files.get(file_id_or_url)

    def download_file(
        self,
        file_id_or_url: str,
        *,
        filename: str | None = None,
        subdir: str | None = None,
        dest: str | None = None,
        if_exists: str = "skip",
    ) -> CommandResult:
        return self._files.download(file_id_or_url, filename=filename, subdir=subdir, dest=dest, if_exists=if_exists)

    def pull_files(
        self,
        *,
        course_id: str | None = None,
        course_query: str | None = None,
        limit: int | None = None,
        subdir: str | None = None,
        dest: str | None = None,
        if_exists: str = "skip",
    ) -> CommandResult:
        return self._files.pull(course_id=course_id, course_query=course_query, limit=limit, subdir=subdir, dest=dest, if_exists=if_exists)

    def list_videos(
        self,
        *,
        course_id: str | None = None,
        course_query: str | None = None,
        limit: int | None = None,
        recent: bool = False,
    ) -> CommandResult:
        return self._videos.list(course_id=course_id, course_query=course_query, limit=limit, recent=recent)

    def show_video(self, video_id_or_url: str, *, course_id_hint: str | None = None) -> CommandResult:
        return self._videos.show(video_id_or_url, course_id_hint=course_id_hint)

    def not_implemented(self, command: str) -> CommandResult:
        raise CommandError(
            code="NOT_IMPLEMENTED",
            message=f"{command} is defined in the v2 interface but not implemented yet.",
            hint="Use the clean-break RFC and implement the provider-backed service behind this command.",
            exit_code=50,
            retryable=False,
        )


def build_container() -> KlmsFacade:
    paths = resolve_paths()
    auth = AuthService(paths)
    assignments = AssignmentService(paths, auth)
    notices = NoticeService(paths, auth)
    files = FileService(paths, auth)
    dashboard = DashboardService(paths, auth, assignments, notices, files)
    sync = SyncService(paths, auth, notices, files)
    probe = CapabilityProbeService(paths, auth)
    capture = EndpointCaptureService(paths, auth)
    courses = CourseService(paths, auth)
    videos = VideoService(paths, auth)
    return KlmsFacade(
        auth=auth,
        assignments=assignments,
        notices=notices,
        files=files,
        dashboard=dashboard,
        sync=sync,
        probe=probe,
        capture=capture,
        courses=courses,
        videos=videos,
    )
