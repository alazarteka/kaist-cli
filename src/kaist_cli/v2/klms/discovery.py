from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qs, urlparse

from .paths import KlmsPaths


def load_json_summary(path: str) -> dict[str, Any] | None:
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def parse_recent_courses_args(summary: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(summary, dict):
        return None
    candidates = summary.get("mapped_endpoints") or summary.get("endpoints") or []
    if not isinstance(candidates, list):
        return None
    for endpoint in candidates:
        if not isinstance(endpoint, dict):
            continue
        methodname = endpoint.get("methodname")
        post_data_preview = endpoint.get("post_data_preview")
        if methodname != "core_course_get_recent_courses" or not isinstance(post_data_preview, str):
            continue
        try:
            payload = json.loads(post_data_preview)
        except Exception:
            continue
        if not isinstance(payload, list) or not payload:
            continue
        first = payload[0]
        if not isinstance(first, dict):
            continue
        args = first.get("args")
        if isinstance(args, dict):
            return args
    return None


def load_recent_courses_args(paths: KlmsPaths, *, limit: int | None = None) -> dict[str, Any]:
    args = (
        parse_recent_courses_args(load_json_summary(str(paths.api_map_path)))
        or parse_recent_courses_args(load_json_summary(str(paths.endpoint_discovery_path)))
        or {}
    )
    resolved = dict(args)
    if limit is not None:
        resolved["limit"] = max(1, int(limit))
    return resolved


def endpoint_canonical_key(method: str, url: str) -> str:
    parsed = urlparse(url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    info = ",".join(sorted(query.get("info", [])))
    path = parsed.path or "/"
    if info:
        return f"{method.upper()} {path}?info={info}"
    return f"{method.upper()} {path}"


def extract_methodname_from_post_data_preview(preview: str) -> str | None:
    text = (preview or "").strip()
    if not text or not text.startswith("["):
        return None
    try:
        data = json.loads(text)
    except Exception:
        return None
    if isinstance(data, list) and data:
        first = data[0]
        if isinstance(first, dict):
            methodname = first.get("methodname")
            if isinstance(methodname, str) and methodname.strip():
                return methodname.strip()
    return None


def summarize_json_shape(value: Any, *, depth: int = 0) -> Any:
    if depth >= 4:
        return {"type": type(value).__name__}
    if isinstance(value, dict):
        keys = list(value.keys())
        sample: dict[str, Any] = {}
        for key in keys[:5]:
            sample[str(key)] = summarize_json_shape(value[key], depth=depth + 1)
        return {
            "type": "object",
            "key_count": len(keys),
            "keys": [str(key) for key in keys[:20]],
            "sample": sample,
        }
    if isinstance(value, list):
        return {
            "type": "array",
            "length": len(value),
            "item_shape": summarize_json_shape(value[0], depth=depth + 1) if value else None,
        }
    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        return {"type": "boolean"}
    if isinstance(value, (int, float)):
        return {"type": "number"}
    if isinstance(value, str):
        return {"type": "string", "length": len(value)}
    return {"type": type(value).__name__}


def classify_endpoint(endpoint: dict[str, Any]) -> dict[str, Any]:
    method = str(endpoint.get("method", "")).upper()
    url = str(endpoint.get("url", ""))
    parsed = urlparse(url)
    path = parsed.path or "/"
    query = parse_qs(parsed.query, keep_blank_values=True)
    info = ",".join(sorted(query.get("info", [])))
    methodname = extract_methodname_from_post_data_preview(str(endpoint.get("post_data_preview") or ""))
    json_like = bool(endpoint.get("json_like"))
    content_types = [str(content_type).lower() for content_type in (endpoint.get("content_types") or [])]
    response_preview = str(endpoint.get("response_preview") or "").lower()

    classification = {
        "canonical_key": endpoint_canonical_key(method, url),
        "path": path,
        "info": info or None,
        "methodname": methodname,
        "category": "unknown",
        "confidence": 0.2,
        "recommended_for_cli": False,
        "reason": "No matching rule.",
    }

    if path == "/lib/ajax/service.php" and "core_course_get_recent_courses" in info:
        classification.update(
            {
                "category": "courses",
                "confidence": 0.95,
                "recommended_for_cli": True,
                "reason": "Core Moodle AJAX recent-courses endpoint.",
            }
        )
        return classification

    if methodname == "core_course_get_recent_courses":
        classification.update(
            {
                "category": "courses",
                "confidence": 0.95,
                "recommended_for_cli": True,
                "reason": "Detected by methodname in AJAX payload.",
            }
        )
        return classification

    if path == "/lib/ajax/service.php" and "core_course_get_enrolled_courses_by_timeline_classification" in info:
        classification.update(
            {
                "category": "courses",
                "confidence": 0.9,
                "recommended_for_cli": True,
                "reason": "Core Moodle timeline/enrolled-courses endpoint.",
            }
        )
        return classification

    if path == "/lib/ajax/service.php" and "core_calendar_get_action_events_by_timesort" in info:
        classification.update(
            {
                "category": "calendar",
                "confidence": 0.85,
                "recommended_for_cli": True,
                "reason": "Core Moodle calendar events endpoint.",
            }
        )
        return classification

    if path == "/lib/ajax/service.php" and "core_course_get_contents" in info:
        if "servicenotavailable" in response_preview:
            classification.update(
                {
                    "category": "files",
                    "confidence": 0.45,
                    "recommended_for_cli": False,
                    "reason": "Core Moodle course contents endpoint exists but the current KLMS instance reports it disabled.",
                }
            )
            return classification
        classification.update(
            {
                "category": "files",
                "confidence": 0.9,
                "recommended_for_cli": True,
                "reason": "Core Moodle course contents endpoint.",
            }
        )
        return classification

    if methodname == "core_course_get_contents":
        if "servicenotavailable" in response_preview:
            classification.update(
                {
                    "category": "files",
                    "confidence": 0.45,
                    "recommended_for_cli": False,
                    "reason": "Detected by methodname in AJAX payload, but the current KLMS instance reports it disabled.",
                }
            )
            return classification
        classification.update(
            {
                "category": "files",
                "confidence": 0.9,
                "recommended_for_cli": True,
                "reason": "Detected by methodname in AJAX payload.",
            }
        )
        return classification

    if methodname and ("courseboard" in methodname or "notice" in methodname):
        classification.update(
            {
                "category": "notices",
                "confidence": 0.82,
                "recommended_for_cli": True,
                "reason": "Methodname indicates notice/courseboard data.",
            }
        )
        return classification

    if methodname and ("assign" in methodname or "calendar" in methodname):
        classification.update(
            {
                "category": "assignments",
                "confidence": 0.82,
                "recommended_for_cli": True,
                "reason": "Methodname indicates assignment/calendar data.",
            }
        )
        return classification

    if path == "/lib/ajax/service.php" and "core_output_load_template_with_dependencies" in info:
        classification.update(
            {
                "category": "ui_template",
                "confidence": 0.7,
                "recommended_for_cli": False,
                "reason": "Template-rendering endpoint, likely presentation-focused.",
            }
        )
        return classification

    if path == "/lib/ajax/service-nologin.php":
        classification.update(
            {
                "category": "ui_template",
                "confidence": 0.6,
                "recommended_for_cli": False,
                "reason": "No-login AJAX template endpoint.",
            }
        )
        return classification

    if "/mod/assign/" in path:
        classification.update(
            {
                "category": "assignments",
                "confidence": 0.8 if json_like else 0.55,
                "recommended_for_cli": json_like,
                "reason": "Assignment module endpoint.",
            }
        )
        return classification

    if "/mod/courseboard/" in path:
        classification.update(
            {
                "category": "notices",
                "confidence": 0.8 if json_like else 0.6,
                "recommended_for_cli": json_like,
                "reason": "Courseboard/notice endpoint.",
            }
        )
        return classification

    if path == "/repository/draftfiles_ajax.php":
        classification.update(
            {
                "category": "files",
                "confidence": 0.55,
                "recommended_for_cli": False,
                "reason": "Draft-files endpoint related to assignment submission editing.",
            }
        )
        return classification

    if "/mod/resource/" in path or "pluginfile.php" in path:
        classification.update(
            {
                "category": "files",
                "confidence": 0.75 if json_like else 0.6,
                "recommended_for_cli": json_like,
                "reason": "Resource/file endpoint.",
            }
        )
        return classification

    if "/panopto/" in path or "video" in path:
        classification.update(
            {
                "category": "video",
                "confidence": 0.7,
                "recommended_for_cli": False,
                "reason": "Video integration endpoint.",
            }
        )
        return classification

    if json_like or any("json" in content_type for content_type in content_types):
        classification.update(
            {
                "category": "json_unknown",
                "confidence": 0.4,
                "recommended_for_cli": False,
                "reason": "JSON-like endpoint; needs manual inspection.",
            }
        )
    return classification


def map_discovery_report(*, report: dict[str, Any], source_report_path: str) -> dict[str, Any]:
    endpoints = report.get("endpoints") or []
    if not isinstance(endpoints, list):
        raise ValueError("Invalid discovery report format: endpoints must be a list.")

    mapped: list[dict[str, Any]] = []
    by_canonical: dict[str, dict[str, Any]] = {}
    for endpoint in endpoints:
        if not isinstance(endpoint, dict):
            continue
        classification = classify_endpoint(endpoint)
        canonical_key = classification["canonical_key"]

        existing = by_canonical.get(canonical_key)
        if existing is None:
            merged = {
                "method": endpoint.get("method"),
                "url": endpoint.get("url"),
                "path": classification["path"],
                "info": classification["info"],
                "methodname": classification.get("methodname"),
                "category": classification["category"],
                "confidence": classification["confidence"],
                "recommended_for_cli": classification["recommended_for_cli"],
                "reason": classification["reason"],
                "seen_count": int(endpoint.get("seen_count", 0) or 0),
                "status_codes": list(endpoint.get("status_codes") or []),
                "content_types": list(endpoint.get("content_types") or []),
                "json_like": bool(endpoint.get("json_like")),
                "request_headers_subset": endpoint.get("request_headers_subset") or {},
                "has_post_data": bool(endpoint.get("has_post_data")),
                "post_data_size": int(endpoint.get("post_data_size", 0) or 0),
                "post_data_preview": endpoint.get("post_data_preview") or "",
                "response_json_shape": endpoint.get("response_json_shape"),
                "response_preview": endpoint.get("response_preview"),
                "canonical_key": canonical_key,
            }
            by_canonical[canonical_key] = merged
            mapped.append(merged)
            continue

        existing["seen_count"] += int(endpoint.get("seen_count", 0) or 0)
        for code in endpoint.get("status_codes") or []:
            if code not in existing["status_codes"]:
                existing["status_codes"].append(code)
        for content_type in endpoint.get("content_types") or []:
            if content_type not in existing["content_types"]:
                existing["content_types"].append(content_type)
        existing["json_like"] = bool(existing["json_like"] or endpoint.get("json_like"))
        if float(classification["confidence"]) > float(existing["confidence"]):
            existing["category"] = classification["category"]
            existing["confidence"] = classification["confidence"]
            existing["recommended_for_cli"] = classification["recommended_for_cli"]
            existing["reason"] = classification["reason"]
            existing["info"] = classification["info"]
            existing["methodname"] = classification.get("methodname")

    mapped.sort(key=lambda item: (0 if item["recommended_for_cli"] else 1, -float(item["confidence"]), -int(item["seen_count"])))

    category_counts: dict[str, int] = {}
    recommended: list[dict[str, Any]] = []
    for item in mapped:
        category = str(item.get("category") or "unknown")
        category_counts[category] = category_counts.get(category, 0) + 1
        if item.get("recommended_for_cli"):
            recommended.append(item)

    return {
        "ok": True,
        "source_report_path": source_report_path,
        "endpoint_count_raw": len(endpoints),
        "endpoint_count_unique": len(mapped),
        "category_counts": category_counts,
        "recommended_count": len(recommended),
        "recommended_endpoints": recommended,
        "mapped_endpoints": mapped,
    }
