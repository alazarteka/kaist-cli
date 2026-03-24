from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any


@dataclass(frozen=True)
class Course:
    id: str
    title: str
    url: str | None
    course_code: str | None
    course_code_base: str | None
    term_label: str | None
    title_variants: tuple[str, ...] = ()
    professors: tuple[str, ...] = ()
    source: str = "unknown"
    confidence: float = 0.0
    auth_mode: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("title_variants", None)
        payload["professors"] = list(self.professors)
        return payload


@dataclass(frozen=True)
class Assignment:
    id: str | None
    title: str
    url: str | None
    due_raw: str | None
    due_iso: str | None
    course_id: str | None
    course_title: str | None
    course_code: str | None
    course_code_base: str | None
    course_title_variants: tuple[str, ...] = ()
    body_text: str | None = None
    body_html: str | None = None
    detail_note: str | None = None
    attachments: tuple[dict[str, Any], ...] = ()
    detail_available: bool = False
    source: str = "unknown"
    confidence: float = 0.0
    auth_mode: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("course_title_variants", None)
        payload["attachments"] = list(self.attachments)
        return payload


@dataclass(frozen=True)
class Notice:
    board_id: str | None
    id: str | None
    title: str
    url: str | None
    posted_raw: str | None
    posted_iso: str | None
    author: str | None = None
    body_text: str | None = None
    body_html: str | None = None
    attachments: tuple[dict[str, Any], ...] = ()
    detail_available: bool = False
    source: str = "unknown"
    confidence: float = 0.0
    auth_mode: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["attachments"] = list(self.attachments)
        return payload


@dataclass(frozen=True)
class FileItem:
    id: str | None
    title: str
    url: str | None
    download_url: str | None
    filename: str | None
    kind: str
    downloadable: bool
    course_id: str | None
    course_title: str | None
    course_code: str | None
    course_code_base: str | None
    source: str = "unknown"
    confidence: float = 0.0
    auth_mode: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Video:
    id: str | None
    title: str
    url: str | None
    viewer_url: str | None
    stream_url: str | None
    course_id: str | None
    course_title: str | None
    course_code: str | None
    course_code_base: str | None
    source: str = "unknown"
    confidence: float = 0.0
    auth_mode: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
