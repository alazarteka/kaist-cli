from __future__ import annotations

from typing import Literal, TypedDict


class BaseEntity(TypedDict, total=False):
    id: str | None
    title: str
    url: str | None
    source: str
    confidence: float
    fetched_at: str


class Course(BaseEntity, total=False):
    id: str
    course_code: str | None
    course_code_base: str | None
    term_label: str | None


class Assignment(BaseEntity, total=False):
    course_id: str | None
    due_raw: str | None
    due_iso: str | None


class Notice(BaseEntity, total=False):
    board_id: str | None
    posted_raw: str | None
    posted_iso: str | None


class Material(BaseEntity, total=False):
    course_id: str | None
    kind: str
    is_video: bool


class InboxItem(BaseEntity, total=False):
    kind: Literal["assignment", "notice", "file"]
    course_id: str | None
    board_id: str | None
    time_iso: str | None
    due_iso: str | None
    posted_iso: str | None
