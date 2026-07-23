from __future__ import annotations

import json
import re
from typing import Any

from bs4 import BeautifulSoup  # type: ignore[import-untyped]

HEADER_LIKE_MARKERS = (
    "ks-header",
    "all-menu",
    "tooltip-layer",
    "breadcrumb",
    "navbar",
    "footer",
    "menu",
)

_SKIP_NOTICE_BOARD_IDS = {"32044", "32045", "32047", "531193"}
_SKIP_NOTICE_BOARD_LABELS = {"notice", "guide to klms", "q&a", "faq"}


def in_header_like_region(element: Any) -> bool:
    current = element
    for _ in range(12):
        if not current or not getattr(current, "attrs", None):
            break
        classes = " ".join(current.attrs.get("class", [])).lower()
        if any(marker in classes for marker in HEADER_LIKE_MARKERS):
            return True
        current = current.parent
    return False


def _norm_label(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().lower()


def discover_notice_board_ids_from_course_page(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    found: list[str] = []
    for anchor in soup.find_all("a", href=True):
        href = str(anchor["href"])
        if "mod/courseboard/view.php" not in href:
            continue
        match = re.search(r"[?&]id=(\d+)", href)
        if not match:
            continue
        board_id = match.group(1)
        label = _norm_label(anchor.get_text(" ", strip=True))
        if anchor.get("target") == "_blank" or in_header_like_region(anchor):
            continue
        if board_id in _SKIP_NOTICE_BOARD_IDS:
            continue
        if label in _SKIP_NOTICE_BOARD_LABELS:
            continue
        found.append(board_id)
    return list(dict.fromkeys(found))


def unwrap_moodle_ajax_data(text: str) -> Any | None:
    payload = unwrap_moodle_ajax_payload(text)
    if payload.get("status") != "ok":
        return None
    return payload.get("data")


def unwrap_moodle_ajax_payload(text: str) -> dict[str, Any]:
    try:
        payload = json.loads(text)
    except Exception as exc:  # noqa: BLE001
        return {"status": "invalid", "message": f"Failed to parse Moodle AJAX JSON: {exc}"}
    if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
        return {"status": "invalid", "message": "Unexpected Moodle AJAX response shape."}
    first = payload[0]
    if bool(first.get("error")):
        exception = first.get("exception") if isinstance(first.get("exception"), dict) else {}
        return {
            "status": "error",
            "error_code": str(exception.get("errorcode") or first.get("errorcode") or "").strip() or None,
            "message": str(
                exception.get("message") or first.get("message") or "Moodle AJAX returned an error payload."
            ).strip(),
            "exception": exception,
        }
    return {"status": "ok", "data": first.get("data")}


def table_col_index(headers_norm: list[str], *needles: str) -> int | None:
    for needle in needles:
        for index, header in enumerate(headers_norm):
            if needle in header:
                return index
    return None


def extend_dict_candidates(candidates: list[dict[str, Any]], items: Any) -> None:
    if isinstance(items, list):
        for item in items:
            if isinstance(item, dict):
                candidates.append(item)
