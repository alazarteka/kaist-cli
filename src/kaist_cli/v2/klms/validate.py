from __future__ import annotations

import re

from bs4 import BeautifulSoup  # type: ignore[import-untyped]


def _norm_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _page_text(html: str) -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    return _norm_text(soup.get_text(" ", strip=True))


def looks_klms_error_html(html: str) -> str | None:
    text = _page_text(html)
    if not text:
        return None

    lowered = text.lower()
    markers = (
        "coding error detected",
        "debug info",
        "stack trace",
        "error reading from database",
        "invalid course module id",
        "invalidcoursemoduleid",
        "cmid is incorrect",
        "required parameter",
        "exception -",
        "more information about this error",
        "call stack",
        "unknown error",
    )
    if any(marker in lowered for marker in markers):
        return text[:300]
    return None
