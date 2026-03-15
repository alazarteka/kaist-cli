from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

from bs4 import BeautifulSoup  # type: ignore[import-untyped]

from ..contracts import CommandResult
from .auth import AuthService, extract_sesskey
from .config import KlmsConfig, abs_url, load_config
from .discovery import map_discovery_report, summarize_json_shape
from .paths import KlmsPaths, chmod_best_effort, ensure_private_dirs


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _same_origin(url_a: str, url_b: str) -> bool:
    try:
        a = urlparse(url_a)
        b = urlparse(url_b)
        return (a.scheme, a.netloc) == (b.scheme, b.netloc)
    except Exception:
        return False


def _dedupe_strings(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def _extract_course_ids_from_dashboard(html: str, *, configured_ids: tuple[str, ...], limit: int) -> list[str]:
    out: list[str] = [str(course_id).strip() for course_id in configured_ids if str(course_id).strip()]
    out.extend(re.findall(r"/course/view\.php\?id=(\d+)", html))
    return _dedupe_strings(out)[: max(0, limit)]


def _discover_notice_board_ids_from_course_page(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    found: list[str] = []

    def in_header_like_region(element: Any) -> bool:
        current = element
        for _ in range(12):
            if not current or not getattr(current, "attrs", None):
                break
            classes = " ".join(current.attrs.get("class", [])).lower()
            if any(
                marker in classes
                for marker in ("ks-header", "all-menu", "tooltip-layer", "breadcrumb", "navbar", "footer", "menu")
            ):
                return True
            current = current.parent
        return False

    for anchor in soup.find_all("a", href=True):
        href = str(anchor["href"])
        if "mod/courseboard/view.php" not in href:
            continue
        match = re.search(r"[?&]id=(\d+)", href)
        if not match:
            continue
        board_id = match.group(1)
        label = anchor.get_text(" ", strip=True).lower()
        if anchor.get("target") == "_blank" or in_header_like_region(anchor):
            continue
        if board_id in {"32044", "32045", "32047", "531193"}:
            continue
        if label in {"notice", "guide to klms", "q&a", "faq"}:
            continue
        found.append(board_id)
    return _dedupe_strings(found)


def _extract_surface_links(html: str, *, base_url: str, per_pattern_limit: int) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    patterns = (
        r"/mod/assign/view\.php\?id=\d+",
        r"/mod/courseboard/article\.php\?[^\"'#\s>]*",
        r"/mod/resource/view\.php\?id=\d+",
    )
    matched: list[str] = []
    counts: dict[str, int] = {pattern: 0 for pattern in patterns}
    for anchor in soup.find_all("a", href=True):
        href = str(anchor["href"]).strip()
        if not href:
            continue
        url = abs_url(base_url, href)
        if not _same_origin(url, base_url):
            continue
        for pattern in patterns:
            if counts[pattern] >= per_pattern_limit:
                continue
            if re.search(pattern, url):
                matched.append(url)
                counts[pattern] += 1
                break
    return _dedupe_strings(matched)


def _write_json_file(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    chmod_best_effort(path, 0o600)


def _unwrap_moodle_ajax_data(text: str) -> Any | None:
    try:
        payload = json.loads(text)
    except Exception:
        return None
    if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
        return None
    first = payload[0]
    if bool(first.get("error")):
        return None
    return first.get("data")


def _moodle_ajax_state(text: str) -> str:
    try:
        payload = json.loads(text)
    except Exception:
        return "unknown"
    if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
        return "unknown"
    first = payload[0]
    if bool(first.get("error")):
        return "error"
    if "data" in first:
        return "success"
    return "unknown"


def _extract_assignment_rows_from_calendar_data(data: Any, *, base_url: str) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []

    def push_list(items: Any) -> None:
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict):
                    candidates.append(item)

    if isinstance(data, dict):
        push_list(data.get("events"))
        push_list(data.get("data"))
        push_list(data.get("items"))
    elif isinstance(data, list):
        push_list(data)

    out: list[dict[str, Any]] = []
    for row in candidates:
        module = str(row.get("modulename") or row.get("modname") or "").lower()
        eventtype = str(row.get("eventtype") or row.get("name") or "").lower()
        if module and module != "assign":
            continue
        if "assign" not in module and "assignment" not in eventtype and "assign" not in eventtype:
            continue
        course_id = str(row.get("courseid") or row.get("course_id") or "").strip() or None
        title = str(row.get("name") or row.get("title") or "assignment").strip() or "assignment"
        url = row.get("url") or row.get("viewurl") or row.get("view_url")
        out.append(
            {
                "course_id": course_id,
                "id": str(row.get("instance") or row.get("id") or "").strip() or None,
                "title": title,
                "url": abs_url(base_url, str(url)) if isinstance(url, str) and url.strip() else None,
            }
        )
    return out


def _hint_endpoint(
    *,
    method: str,
    url: str,
    post_data_preview: str,
    response_preview: str,
) -> dict[str, Any]:
    return {
        "method": method,
        "url": url,
        "resource_type": "hint",
        "seen_count": 0,
        "request_headers_subset": {
            "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
            "x-requested-with": "XMLHttpRequest",
        },
        "has_post_data": bool(post_data_preview),
        "post_data_size": len(post_data_preview),
        "post_data_preview": post_data_preview,
        "status_codes": [],
        "content_types": [],
        "json_like": False,
        "response_preview": response_preview,
        "response_json_shape": None,
        "hint_only": True,
    }


def _extract_courseboard_js_hints(script_text: str, *, base_url: str) -> list[dict[str, Any]]:
    hints: list[dict[str, Any]] = []
    types = set(re.findall(r"type=([a-zA-Z0-9_]+)", script_text))
    if "comment_info" in types:
        hints.append(
            _hint_endpoint(
                method="POST",
                url=abs_url(base_url, "/mod/courseboard/ajax.php"),
                post_data_preview="type=comment_info&cmid=<comment_id>",
                response_preview="Discovered in mod/courseboard/module.js as a read-only comment lookup.",
            )
        )
    if "category_sortable" in types:
        hints.append(
            _hint_endpoint(
                method="POST",
                url=abs_url(base_url, "/mod/courseboard/action.php"),
                post_data_preview="type=category_sortable&idx=<order>&id=<cm_id>&cid=<course_id>&bid=<board_id>",
                response_preview="Discovered in mod/courseboard/module.js; likely mutates board category ordering.",
            )
        )
    return hints


_COURSEBOARD_RUNTIME_CAPTURE_JS = r"""
(() => {
  const STORAGE_KEY = "__kaistCourseboardCaptureLogs";
  const MAX_EVENTS = 200;

  function safeLoad() {
    try {
      const raw = window.sessionStorage.getItem(STORAGE_KEY);
      if (!raw) {
        return [];
      }
      const parsed = JSON.parse(raw);
      return Array.isArray(parsed) ? parsed : [];
    } catch (_error) {
      return [];
    }
  }

  function safeSave(events) {
    try {
      window.sessionStorage.setItem(STORAGE_KEY, JSON.stringify(events.slice(-MAX_EVENTS)));
    } catch (_error) {
      // Ignore storage failures.
    }
  }

  function preview(value) {
    if (value == null) {
      return "";
    }
    if (typeof value === "string") {
      return value.slice(0, 400);
    }
    if (typeof URLSearchParams !== "undefined" && value instanceof URLSearchParams) {
      return value.toString().slice(0, 400);
    }
    if (typeof FormData !== "undefined" && value instanceof FormData) {
      const pairs = [];
      for (const [key, entry] of value.entries()) {
        pairs.push(`${key}=${String(entry)}`);
        if (pairs.length >= 10) {
          break;
        }
      }
      return pairs.join("&").slice(0, 400);
    }
    if (typeof Blob !== "undefined" && value instanceof Blob) {
      return `[blob:${value.type || "application/octet-stream"}:${value.size}]`;
    }
    if (typeof ArrayBuffer !== "undefined" && value instanceof ArrayBuffer) {
      return `[arraybuffer:${value.byteLength}]`;
    }
    try {
      return JSON.stringify(value).slice(0, 400);
    } catch (_error) {
      return String(value).slice(0, 400);
    }
  }

  function headerObject(headers) {
    const out = {};
    if (!headers) {
      return out;
    }
    try {
      if (typeof Headers !== "undefined" && headers instanceof Headers) {
        headers.forEach((value, key) => {
          out[String(key)] = String(value);
        });
        return out;
      }
      if (Array.isArray(headers)) {
        for (const pair of headers) {
          if (Array.isArray(pair) && pair.length >= 2) {
            out[String(pair[0])] = String(pair[1]);
          }
        }
        return out;
      }
      if (typeof headers === "object") {
        for (const [key, value] of Object.entries(headers)) {
          out[String(key)] = String(value);
        }
      }
    } catch (_error) {
      return out;
    }
    return out;
  }

  function absoluteUrl(value) {
    try {
      return new URL(String(value || ""), window.location.href).href;
    } catch (_error) {
      return String(value || "");
    }
  }

  if (window.__kaistCourseboardCapture && window.__kaistCourseboardCapture.__installed) {
    return;
  }

  const state = {
    events: safeLoad(),
    seq: 0,
  };

  function nextId() {
    state.seq += 1;
    return `cb-${Date.now()}-${state.seq}`;
  }

  function push(event) {
    if (!event || typeof event !== "object") {
      return;
    }
    const normalized = {
      timestamp: new Date().toISOString(),
      ...event,
    };
    if (!normalized.requestId) {
      normalized.requestId = nextId();
    }
    state.events.push(normalized);
    if (state.events.length > MAX_EVENTS) {
      state.events = state.events.slice(-MAX_EVENTS);
    }
    safeSave(state.events);
  }

  function installFetch() {
    if (typeof window.fetch !== "function" || window.fetch.__kaistWrapped) {
      return;
    }
    const original = window.fetch.bind(window);
    const wrapped = async (input, init) => {
      const requestId = nextId();
      let method = "GET";
      let url = "";
      let headers = {};
      let bodyPreview = "";
      try {
        if (typeof Request !== "undefined" && input instanceof Request) {
          method = String(input.method || method).toUpperCase();
          url = input.url || "";
          headers = headerObject(input.headers);
        } else {
          url = String(input || "");
        }
        if (init && init.method) {
          method = String(init.method).toUpperCase();
        }
        if (init && init.headers) {
          headers = headerObject(init.headers);
        }
        if (init && Object.prototype.hasOwnProperty.call(init, "body")) {
          bodyPreview = preview(init.body);
        }
      } catch (_error) {
        // Best-effort logging only.
      }
      push({
        requestId,
        transport: "fetch",
        phase: "request",
        method,
        url: absoluteUrl(url),
        requestHeaders: headers,
        postDataPreview: bodyPreview,
      });
      try {
        const response = await original(input, init);
        let responsePreview = "";
        try {
          responsePreview = await response.clone().text();
        } catch (_error) {
          responsePreview = "";
        }
        push({
          requestId,
          transport: "fetch",
          phase: "response",
          method,
          url: absoluteUrl(response.url || url),
          status: Number(response.status || 0),
          contentType: response.headers.get("content-type") || "",
          responsePreview: preview(responsePreview),
        });
        return response;
      } catch (error) {
        push({
          requestId,
          transport: "fetch",
          phase: "error",
          method,
          url: absoluteUrl(url),
          error: String(error),
        });
        throw error;
      }
    };
    wrapped.__kaistWrapped = true;
    window.fetch = wrapped;
  }

  function installXHR() {
    if (typeof XMLHttpRequest === "undefined") {
      return;
    }
    const proto = XMLHttpRequest.prototype;
    if (proto.send && proto.send.__kaistWrapped) {
      return;
    }
    const open = proto.open;
    const send = proto.send;
    const setRequestHeader = proto.setRequestHeader;

    proto.open = function(method, url) {
      this.__kaistCapture = {
        requestId: nextId(),
        method: String(method || "GET").toUpperCase(),
        url: absoluteUrl(url),
        requestHeaders: {},
      };
      return open.apply(this, arguments);
    };

    proto.setRequestHeader = function(name, value) {
      if (this.__kaistCapture && name) {
        this.__kaistCapture.requestHeaders[String(name)] = String(value);
      }
      return setRequestHeader.apply(this, arguments);
    };

    proto.send = function(body) {
      const meta = this.__kaistCapture || {
        requestId: nextId(),
        method: "GET",
        url: absoluteUrl(""),
        requestHeaders: {},
      };
      push({
        requestId: meta.requestId,
        transport: "xhr",
        phase: "request",
        method: meta.method,
        url: meta.url,
        requestHeaders: meta.requestHeaders || {},
        postDataPreview: preview(body),
      });
      this.addEventListener(
        "loadend",
        () => {
          push({
            requestId: meta.requestId,
            transport: "xhr",
            phase: "response",
            method: meta.method,
            url: absoluteUrl(this.responseURL || meta.url),
            status: Number(this.status || 0),
            contentType: this.getResponseHeader("content-type") || "",
            responsePreview: preview(this.responseText || ""),
          });
        },
        {once: true},
      );
      return send.apply(this, arguments);
    };

    proto.send.__kaistWrapped = true;
  }

  function installJQuery() {
    const jq = window.jQuery || window.$;
    if (!jq || typeof jq.ajax !== "function" || jq.ajax.__kaistWrapped) {
      return false;
    }
    const original = jq.ajax.bind(jq);
    const wrapped = function(urlOrOptions, maybeOptions) {
      const requestId = nextId();
      const options =
        typeof urlOrOptions === "string"
          ? {...(maybeOptions || {}), url: urlOrOptions}
          : {...(urlOrOptions || {})};
      const method = String(options.type || options.method || "GET").toUpperCase();
      const url = absoluteUrl(options.url || "");
      push({
        requestId,
        transport: "jquery_ajax",
        phase: "config",
        method,
        url,
        requestHeaders: headerObject(options.headers),
        postDataPreview: preview(options.data),
        dataType: String(options.dataType || ""),
      });
      const jqxhr = original(urlOrOptions, maybeOptions);
      if (jqxhr && typeof jqxhr.done === "function") {
        jqxhr.done((data, textStatus, xhr) => {
          push({
            requestId,
            transport: "jquery_ajax",
            phase: "response",
            method,
            url: absoluteUrl((xhr && xhr.responseURL) || url),
            status: Number((xhr && xhr.status) || 200),
            contentType: (xhr && xhr.getResponseHeader && xhr.getResponseHeader("content-type")) || "",
            responsePreview: preview(data),
            textStatus: String(textStatus || ""),
          });
        });
      }
      if (jqxhr && typeof jqxhr.fail === "function") {
        jqxhr.fail((xhr, textStatus, errorThrown) => {
          push({
            requestId,
            transport: "jquery_ajax",
            phase: "error",
            method,
            url: absoluteUrl((xhr && xhr.responseURL) || url),
            status: Number((xhr && xhr.status) || 0),
            contentType: (xhr && xhr.getResponseHeader && xhr.getResponseHeader("content-type")) || "",
            responsePreview: preview(xhr && (xhr.responseText || "")),
            textStatus: String(textStatus || ""),
            error: String(errorThrown || ""),
          });
        });
      }
      return jqxhr;
    };
    wrapped.__kaistWrapped = true;
    jq.ajax = wrapped;
    return true;
  }

  installFetch();
  installXHR();
  if (!installJQuery()) {
    const timer = window.setInterval(() => {
      try {
        if (installJQuery()) {
          window.clearInterval(timer);
        }
      } catch (_error) {
        // Ignore patch retries.
      }
    }, 500);
  }

  document.addEventListener(
    "click",
    (event) => {
      const target = event.target && event.target.closest ? event.target.closest("a[href]") : null;
      if (!target) {
        return;
      }
      const href = absoluteUrl(target.getAttribute("href") || target.href || "");
      if (!href || href.indexOf("/mod/courseboard/") === -1) {
        return;
      }
      push({
        transport: "dom_click",
        phase: "event",
        url: href,
        method: "GET",
        label: preview(target.textContent || ""),
      });
    },
    true,
  );

  document.addEventListener(
    "submit",
    (event) => {
      const form = event.target;
      if (!form || !form.getAttribute) {
        return;
      }
      const action = absoluteUrl(form.getAttribute("action") || window.location.href);
      if (action.indexOf("/mod/courseboard/") === -1) {
        return;
      }
      let payload = "";
      try {
        payload = preview(new FormData(form));
      } catch (_error) {
        payload = "";
      }
      push({
        transport: "form",
        phase: "submit",
        url: action,
        method: String(form.getAttribute("method") || "GET").toUpperCase(),
        postDataPreview: payload,
      });
    },
    true,
  );

  window.__kaistCourseboardCapture = {
    __installed: true,
    peek() {
      return state.events.slice();
    },
    drain() {
      const out = state.events.slice();
      state.events = [];
      safeSave(state.events);
      return out;
    },
    clear() {
      state.events = [];
      safeSave(state.events);
    },
    push,
  };
})();
"""


def _runtime_response_shape(preview: str) -> dict[str, Any] | None:
    text = str(preview or "").strip()
    if not text:
        return None
    try:
        return summarize_json_shape(json.loads(text))
    except Exception:
        return None


def _runtime_event_sample(event: dict[str, Any]) -> dict[str, Any]:
    sample: dict[str, Any] = {}
    for key in (
        "requestId",
        "transport",
        "phase",
        "method",
        "url",
        "status",
        "contentType",
        "postDataPreview",
        "responsePreview",
        "label",
        "textStatus",
        "error",
        "timestamp",
    ):
        value = event.get(key)
        if value is None or value == "":
            continue
        if key in {"postDataPreview", "responsePreview", "label", "error"}:
            sample[key] = str(value)[:200]
        else:
            sample[key] = value
    return sample


def _merge_runtime_pair(existing: dict[str, Any], event: dict[str, Any]) -> None:
    phase = str(event.get("phase") or "")
    if phase in {"request", "config", "submit", "event"}:
        if not existing.get("method") and event.get("method"):
            existing["method"] = str(event.get("method"))
        if not existing.get("url") and event.get("url"):
            existing["url"] = str(event.get("url"))
        if not existing.get("postDataPreview") and event.get("postDataPreview"):
            existing["postDataPreview"] = str(event.get("postDataPreview"))
        headers = event.get("requestHeaders")
        if isinstance(headers, dict) and not existing.get("requestHeaders"):
            existing["requestHeaders"] = headers
    if phase == "response":
        if event.get("status") is not None:
            existing["status"] = int(event.get("status") or 0)
        if event.get("contentType"):
            existing["contentType"] = str(event.get("contentType"))
        if event.get("responsePreview"):
            existing["responsePreview"] = str(event.get("responsePreview"))
    if phase == "error" and event.get("error"):
        existing["error"] = str(event.get("error"))


def _courseboard_runtime_capture_summary(events: list[dict[str, Any]], *, base_url: str) -> dict[str, Any]:
    request_like = {"request", "config", "submit", "event"}
    paired: dict[str, dict[str, Any]] = {}
    transport_counts: dict[str, int] = {}
    sample_events: list[dict[str, Any]] = []
    observed_paths: list[str] = []

    for raw_event in events:
        if not isinstance(raw_event, dict):
            continue
        event = dict(raw_event)
        transport = str(event.get("transport") or "unknown")
        phase = str(event.get("phase") or "")
        transport_counts[transport] = int(transport_counts.get(transport, 0)) + 1
        if len(sample_events) < 12:
            sample_events.append(_runtime_event_sample(event))

        url = str(event.get("url") or "").strip()
        if url and _same_origin(url, base_url):
            path = urlparse(url).path or "/"
            if path and path not in observed_paths:
                observed_paths.append(path)

        request_id = str(event.get("requestId") or "").strip()
        if not request_id:
            request_id = f"{transport}:{phase}:{url}:{len(paired)}"
        pair = paired.setdefault(request_id, {"transport": transport, "phase_history": []})
        pair["transport"] = transport
        pair["phase_history"].append(phase)
        _merge_runtime_pair(pair, event)

    endpoints: list[dict[str, Any]] = []
    for pair in paired.values():
        url = str(pair.get("url") or "").strip()
        if not url or not _same_origin(url, base_url):
            continue
        path = urlparse(url).path or "/"
        if "/mod/courseboard/" not in path and path != "/lib/ajax/service.php":
            continue
        response_preview = str(pair.get("responsePreview") or "")
        content_type = str(pair.get("contentType") or "")
        json_like = "json" in content_type.lower() or _runtime_response_shape(response_preview) is not None
        endpoint: dict[str, Any] = {
            "method": str(pair.get("method") or "GET").upper(),
            "url": url,
            "resource_type": str(pair.get("transport") or "runtime"),
            "seen_count": 1,
            "request_headers_subset": {
                key: value
                for key, value in (pair.get("requestHeaders") or {}).items()
                if str(key).lower() in {"content-type", "accept", "x-requested-with", "referer"}
            },
            "has_post_data": bool(pair.get("postDataPreview")),
            "post_data_size": len(str(pair.get("postDataPreview") or "")),
            "post_data_preview": str(pair.get("postDataPreview") or "")[:400],
            "status_codes": [int(pair.get("status"))] if pair.get("status") is not None else [],
            "content_types": [content_type] if content_type else [],
            "json_like": json_like,
            "response_preview": response_preview[:400],
            "response_json_shape": _runtime_response_shape(response_preview) if json_like else None,
            "hint_only": "response" not in set(pair.get("phase_history") or []),
        }
        endpoints.append(endpoint)

    return {
        "event_count": len([event for event in events if isinstance(event, dict)]),
        "request_event_count": len(
            [event for event in events if isinstance(event, dict) and str(event.get("phase") or "") in request_like]
        ),
        "response_event_count": len(
            [event for event in events if isinstance(event, dict) and str(event.get("phase") or "") == "response"]
        ),
        "transport_counts": transport_counts,
        "observed_paths": observed_paths[:20],
        "sample_events": sample_events,
        "endpoints": endpoints,
    }


class EndpointCaptureService:
    def __init__(self, paths: KlmsPaths, auth: AuthService) -> None:
        self._paths = paths
        self._auth = auth

    def discover(
        self,
        *,
        max_courses: int = 2,
        max_notice_boards: int = 2,
        per_surface_links: int = 2,
        manual_courseboard_seconds: int = 0,
    ) -> CommandResult:
        ensure_private_dirs(self._paths)
        config = load_config(self._paths)
        max_courses = max(0, min(max_courses, 10))
        max_notice_boards = max(0, min(max_notice_boards, 10))
        per_surface_links = max(0, min(per_surface_links, 5))
        manual_courseboard_seconds = max(0, min(int(manual_courseboard_seconds), 300))

        def callback(context: Any, auth_mode: str) -> CommandResult:
            report = self._discover_with_context(
                context=context,
                config=config,
                auth_mode=auth_mode,
                max_courses=max_courses,
                max_notice_boards=max_notice_boards,
                per_surface_links=per_surface_links,
                manual_courseboard_seconds=manual_courseboard_seconds,
            )
            _write_json_file(self._paths.endpoint_discovery_path, report)
            api_map = map_discovery_report(report=report, source_report_path=str(self._paths.endpoint_discovery_path))
            api_map["generated_at_iso"] = _utc_now_iso()
            _write_json_file(self._paths.api_map_path, api_map)
            summary = {
                "report_path": str(self._paths.endpoint_discovery_path),
                "map_path": str(self._paths.api_map_path),
                "visited_urls": report["visited_urls"],
                "visit_errors": report["visit_errors"],
                "course_ids_used": report["course_ids_used"],
                "board_ids_used": report["board_ids_used"],
                "targeted_probe_results": report["targeted_probe_results"],
                "endpoint_count_raw": report["endpoint_count"],
                "endpoint_count_unique": api_map["endpoint_count_unique"],
                "category_counts": api_map["category_counts"],
                "recommended_endpoints": api_map["recommended_endpoints"],
            }
            return CommandResult(data=summary, source="probe", capability="full")

        return self._auth.run_authenticated(
            config=config,
            headless=manual_courseboard_seconds <= 0,
            accept_downloads=False,
            timeout_seconds=10.0,
            callback=callback,
        )

    def _discover_with_context(
        self,
        *,
        context: Any,
        config: KlmsConfig,
        auth_mode: str,
        max_courses: int,
        max_notice_boards: int,
        per_surface_links: int,
        manual_courseboard_seconds: int,
    ) -> dict[str, Any]:
        captured: dict[str, dict[str, Any]] = {}
        visited_urls: list[str] = []
        visit_errors: list[dict[str, str]] = []
        targeted_probe_results: list[dict[str, Any]] = []
        base_url = config.base_url.rstrip("/")

        def merge_endpoint(entry: dict[str, Any]) -> None:
            key = f"{entry.get('method')} {entry.get('url')}"
            existing = captured.get(key)
            if existing is None:
                captured[key] = dict(entry)
                return
            existing["seen_count"] = int(existing.get("seen_count", 0) or 0) + int(entry.get("seen_count", 0) or 0)
            for field in ("status_codes", "content_types"):
                values = list(existing.get(field) or [])
                for value in entry.get(field) or []:
                    if value not in values:
                        values.append(value)
                existing[field] = values
            if not existing.get("response_preview") and entry.get("response_preview"):
                existing["response_preview"] = entry["response_preview"]
            if existing.get("response_json_shape") is None and entry.get("response_json_shape") is not None:
                existing["response_json_shape"] = entry["response_json_shape"]
            if not existing.get("hint_only") and entry.get("hint_only"):
                existing["hint_only"] = True

        def on_request(request: Any) -> None:
            if request.resource_type not in {"xhr", "fetch"}:
                return
            if not _same_origin(request.url, base_url):
                return
            key = f"{request.method} {request.url}"
            item = captured.get(key)
            if item is None:
                item = {
                    "method": request.method,
                    "url": request.url,
                    "resource_type": request.resource_type,
                    "seen_count": 0,
                    "request_headers_subset": {
                        key: value
                        for key, value in (request.headers or {}).items()
                        if key.lower() in {"content-type", "accept", "x-requested-with", "referer"}
                    },
                    "has_post_data": bool(request.post_data),
                    "post_data_size": len(request.post_data or ""),
                    "post_data_preview": (request.post_data or "")[:400],
                    "status_codes": [],
                    "content_types": [],
                    "json_like": False,
                    "response_preview": "",
                    "response_json_shape": None,
                }
                captured[key] = item
            item["seen_count"] += 1

        def on_response(response: Any) -> None:
            request = response.request
            if request.resource_type not in {"xhr", "fetch"}:
                return
            if not _same_origin(request.url, base_url):
                return
            key = f"{request.method} {request.url}"
            item = captured.get(key)
            if item is None:
                return
            status_code = int(response.status)
            if status_code not in item["status_codes"]:
                item["status_codes"].append(status_code)
            content_type = str((response.headers or {}).get("content-type", ""))
            if content_type and content_type not in item["content_types"]:
                item["content_types"].append(content_type)
            if "json" not in content_type.lower():
                return
            try:
                text = response.text()
            except Exception:
                return
            current_state = _moodle_ajax_state(str(item.get("response_preview") or ""))
            next_state = _moodle_ajax_state(text)
            should_replace = item.get("response_json_shape") is None or (current_state == "error" and next_state == "success")
            item["json_like"] = True
            if should_replace:
                item["response_preview"] = text[:400]
                try:
                    item["response_json_shape"] = summarize_json_shape(json.loads(text))
                except Exception:
                    item["response_json_shape"] = {"type": "non-json-text", "length": len(text)}

        context.on("request", on_request)
        context.on("response", on_response)
        try:
            dashboard_html = self._visit_page(
                context=context,
                target=abs_url(config.base_url, config.dashboard_path),
                visited_urls=visited_urls,
                visit_errors=visit_errors,
            )
            course_ids = _extract_course_ids_from_dashboard(
                dashboard_html or "",
                configured_ids=config.course_ids,
                limit=max_courses,
            )

            queue: list[str] = [abs_url(config.base_url, config.dashboard_path)]
            board_ids: list[str] = [str(board_id).strip() for board_id in config.notice_board_ids if str(board_id).strip()]
            board_ids = _dedupe_strings(board_ids)[:max_notice_boards]

            for course_id in course_ids:
                course_url = abs_url(config.base_url, f"/course/view.php?id={course_id}&section=0")
                queue.append(course_url)
                queue.append(abs_url(config.base_url, f"/mod/assign/index.php?id={course_id}"))
                queue.append(abs_url(config.base_url, f"/mod/resource/index.php?id={course_id}"))
                course_html = self._visit_page(
                    context=context,
                    target=course_url,
                    visited_urls=visited_urls,
                    visit_errors=visit_errors,
                )
                if course_html:
                    queue.extend(_extract_surface_links(course_html, base_url=config.base_url, per_pattern_limit=per_surface_links))
                    board_ids.extend(_discover_notice_board_ids_from_course_page(course_html))

            board_ids = _dedupe_strings(board_ids)[:max_notice_boards]
            for board_id in board_ids:
                queue.append(abs_url(config.base_url, f"/mod/courseboard/view.php?id={board_id}"))

            queue.extend(
                [
                    abs_url(config.base_url, "/calendar/view.php?view=month"),
                    abs_url(config.base_url, "/calendar/view.php?view=upcoming"),
                ]
            )

            seen_targets: set[str] = set()
            for target in _dedupe_strings(queue):
                if target in seen_targets:
                    continue
                seen_targets.add(target)
                html = self._visit_page(
                    context=context,
                    target=target,
                    visited_urls=visited_urls,
                    visit_errors=visit_errors,
                )
                if html:
                    for href in _extract_surface_links(html, base_url=config.base_url, per_pattern_limit=per_surface_links):
                        if href in seen_targets:
                            continue
                        seen_targets.add(href)
                        self._visit_page(
                            context=context,
                            target=href,
                            visited_urls=visited_urls,
                            visit_errors=visit_errors,
                        )

            targeted_probe_results.extend(
                self._run_targeted_probes(
                    context=context,
                    config=config,
                    dashboard_html=dashboard_html or "",
                    course_ids=course_ids,
                    board_ids=board_ids,
                    manual_courseboard_seconds=manual_courseboard_seconds,
                    merge_endpoint=merge_endpoint,
                )
            )
        finally:
            try:
                context.remove_listener("request", on_request)
            except Exception:
                pass
            try:
                context.remove_listener("response", on_response)
            except Exception:
                pass

        endpoints = list(captured.values())
        endpoints.sort(key=lambda item: (0 if item.get("json_like") else 1, -int(item.get("seen_count", 0)), str(item.get("url", ""))))
        return {
            "ok": True,
            "generated_at_iso": _utc_now_iso(),
            "base_url": config.base_url,
            "auth_mode": auth_mode,
            "visited_urls": visited_urls,
            "visit_errors": visit_errors,
            "course_ids_used": course_ids,
            "board_ids_used": board_ids,
            "targeted_probe_results": targeted_probe_results,
            "endpoint_count": len(endpoints),
            "endpoints": endpoints,
        }

    def _run_targeted_probes(
        self,
        *,
        context: Any,
        config: KlmsConfig,
        dashboard_html: str,
        course_ids: list[str],
        board_ids: list[str],
        manual_courseboard_seconds: int,
        merge_endpoint: Any,
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        sesskey = extract_sesskey(dashboard_html)
        if sesskey:
            results.append(self._probe_calendar_events(context=context, config=config, sesskey=sesskey))
            results.append(self._probe_course_contents(context=context, config=config, sesskey=sesskey, course_ids=course_ids))
        else:
            results.append(
                {
                    "probe": "core_calendar_get_action_events_by_timesort",
                    "status": "skipped",
                    "reason": "No sesskey found on dashboard HTML.",
                }
            )
            results.append(
                {
                    "probe": "core_course_get_contents",
                    "status": "skipped",
                    "reason": "No sesskey found on dashboard HTML.",
                }
            )

        courseboard_hints = self._probe_courseboard_js_hints(context=context, config=config, board_ids=board_ids)
        for endpoint in courseboard_hints.get("endpoints", []):
            merge_endpoint(endpoint)
        results.append(
            {
                "probe": "courseboard_module_js",
                "status": courseboard_hints["status"],
                "hint_count": len(courseboard_hints.get("endpoints", [])),
                "types": courseboard_hints.get("types", []),
                "script_url": courseboard_hints.get("script_url"),
            }
        )
        courseboard_runtime = self._probe_courseboard_runtime(
            context=context,
            config=config,
            board_ids=board_ids,
            manual_seconds=manual_courseboard_seconds,
        )
        for endpoint in courseboard_runtime.get("endpoints", []):
            merge_endpoint(endpoint)
        results.append(
            {
                "probe": "courseboard_runtime",
                "status": courseboard_runtime.get("status"),
                "board_url": courseboard_runtime.get("board_url"),
                "actions": courseboard_runtime.get("actions", []),
                "dom_summary": courseboard_runtime.get("dom_summary"),
                "event_count": courseboard_runtime.get("event_count", 0),
                "request_event_count": courseboard_runtime.get("request_event_count", 0),
                "response_event_count": courseboard_runtime.get("response_event_count", 0),
                "transport_counts": courseboard_runtime.get("transport_counts", {}),
                "observed_paths": courseboard_runtime.get("observed_paths", []),
                "sample_events": courseboard_runtime.get("sample_events", []),
                "endpoint_count": len(courseboard_runtime.get("endpoints", [])),
                "manual_capture_seconds": courseboard_runtime.get("manual_capture_seconds", 0),
                "reason": courseboard_runtime.get("reason"),
                "error": courseboard_runtime.get("error"),
            }
        )
        return results

    def _probe_calendar_events(self, *, context: Any, config: KlmsConfig, sesskey: str) -> dict[str, Any]:
        page = context.new_page()
        try:
            page.goto(abs_url(config.base_url, config.dashboard_path), wait_until="domcontentloaded", timeout=30_000)
            methodname = "core_calendar_get_action_events_by_timesort"
            ajax_url = abs_url(
                config.base_url,
                f"/lib/ajax/service.php?sesskey={quote(sesskey)}&info={quote(methodname)}",
            )
            args_candidates = [
                {"limitnum": 200, "timesortfrom": 0},
                {"limitnum": 200, "timesortfrom": 0, "limit": 200},
                {},
            ]
            for args in args_candidates:
                payload = [{"index": 0, "methodname": methodname, "args": args}]
                result = page.evaluate(
                    """
                    async ({url, payload}) => {
                      const response = await fetch(url, {
                        method: "POST",
                        headers: {
                          "Content-Type": "application/json",
                          "X-Requested-With": "XMLHttpRequest",
                          "Accept": "application/json, text/javascript, */*; q=0.01"
                        },
                        body: JSON.stringify(payload),
                        credentials: "same-origin"
                      });
                      const text = await response.text();
                      return {
                        ok: response.ok,
                        status: response.status,
                        url: response.url,
                        contentType: response.headers.get("content-type") || "",
                        text
                      };
                    }
                    """,
                    {"url": ajax_url, "payload": payload},
                )
                data = _unwrap_moodle_ajax_data(str(result.get("text") or ""))
                if data is None:
                    continue
                assignments = _extract_assignment_rows_from_calendar_data(data, base_url=config.base_url)
                return {
                    "probe": methodname,
                    "status": "ok",
                    "args_used": args,
                    "assignment_count": len(assignments),
                    "sample_assignment": assignments[0] if assignments else None,
                    "content_type": result.get("contentType"),
                }
            return {
                "probe": methodname,
                "status": "no_match",
                "reason": "Endpoint responded but no usable assignment/calendar payload was extracted.",
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "probe": "core_calendar_get_action_events_by_timesort",
                "status": "error",
                "error": str(exc),
            }
        finally:
            page.close()

    def _probe_courseboard_js_hints(self, *, context: Any, config: KlmsConfig, board_ids: list[str]) -> dict[str, Any]:
        if not board_ids:
            return {"status": "skipped", "reason": "No notice boards discovered.", "endpoints": [], "types": []}

        board_page = context.new_page()
        script_page = context.new_page()
        try:
            board_page.goto(
                abs_url(config.base_url, f"/mod/courseboard/view.php?id={board_ids[0]}"),
                wait_until="domcontentloaded",
                timeout=30_000,
            )
            script_urls = board_page.eval_on_selector_all("script[src]", "els => els.map(el => el.src)")
            courseboard_scripts = [
                script_url
                for script_url in script_urls
                if isinstance(script_url, str) and "/mod/courseboard/" in script_url
            ]
            if not courseboard_scripts:
                return {"status": "skipped", "reason": "No courseboard scripts found.", "endpoints": [], "types": []}

            module_url = next(
                (script_url for script_url in courseboard_scripts if script_url.endswith("/mod/courseboard/module.js")),
                courseboard_scripts[0],
            )
            script_page.goto(module_url, wait_until="domcontentloaded", timeout=30_000)
            script_text = script_page.text_content("body") or ""
            endpoints = _extract_courseboard_js_hints(script_text, base_url=config.base_url)
            return {
                "status": "ok" if endpoints else "no_match",
                "script_url": module_url,
                "types": sorted(set(re.findall(r"type=([a-zA-Z0-9_]+)", script_text))),
                "endpoints": endpoints,
            }
        except Exception as exc:  # noqa: BLE001
            return {"status": "error", "error": str(exc), "endpoints": [], "types": []}
        finally:
            board_page.close()
            script_page.close()

    def _probe_courseboard_runtime(
        self,
        *,
        context: Any,
        config: KlmsConfig,
        board_ids: list[str],
        manual_seconds: int = 0,
    ) -> dict[str, Any]:
        if not board_ids:
            return {"status": "skipped", "reason": "No notice boards discovered.", "endpoints": []}

        manual_seconds = max(0, min(int(manual_seconds), 300))
        page = context.new_page()
        actions: list[dict[str, Any]] = []
        try:
            page.add_init_script(_COURSEBOARD_RUNTIME_CAPTURE_JS)
            board_url = abs_url(config.base_url, f"/mod/courseboard/view.php?id={board_ids[0]}")
            page.goto(board_url, wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_timeout(700)
            actions.append({"action": "open_board", "url": page.url})
            dom_summary = self._courseboard_dom_summary(page)
            if manual_seconds > 0:
                try:
                    page.bring_to_front()
                except Exception:
                    pass
                actions.append(
                    {
                        "action": "manual_capture_window",
                        "status": "waiting",
                        "seconds": manual_seconds,
                        "instruction": "Use the visible browser tab and stay in the same tab while clicking board/article UI.",
                    }
                )
                page.wait_for_timeout(manual_seconds * 1000)
            else:
                if int(dom_summary.get("comment_info_trigger_count", 0) or 0) > 0:
                    clicked = self._click_selector(
                        page,
                        '[onclick*="comment_info"], [href*="comment_info"], [data-type="comment_info"]',
                        wait_for_navigation=False,
                        settle_ms=700,
                    )
                    actions.append({"action": "trigger_comment_info", "status": "ok" if clicked else "skipped"})

                article_opened = False
                article_href = self._selector_href(page, 'a[href*="/mod/courseboard/article.php"]')
                if article_href:
                    try:
                        page.goto(article_href, wait_until="domcontentloaded", timeout=30_000)
                        page.wait_for_timeout(700)
                        article_opened = True
                        actions.append({"action": "open_article", "status": "ok", "url": page.url})
                    except Exception as exc:  # noqa: BLE001
                        actions.append({"action": "open_article", "status": "error", "error": str(exc)})
                else:
                    actions.append({"action": "open_article", "status": "skipped"})

                if article_opened:
                    try:
                        page.go_back(wait_until="domcontentloaded", timeout=30_000)
                        page.wait_for_timeout(700)
                        actions.append({"action": "back_to_board", "status": "ok", "url": page.url})
                    except Exception as exc:  # noqa: BLE001
                        actions.append({"action": "back_to_board", "status": "error", "error": str(exc)})

                pagination_href = self._selector_href(page, 'a[href*="/mod/courseboard/view.php"][href*="page="]')
                if pagination_href:
                    try:
                        page.goto(pagination_href, wait_until="domcontentloaded", timeout=30_000)
                        page.wait_for_timeout(700)
                        actions.append({"action": "open_pagination", "status": "ok", "url": page.url})
                    except Exception as exc:  # noqa: BLE001
                        actions.append({"action": "open_pagination", "status": "error", "error": str(exc)})
                else:
                    actions.append({"action": "open_pagination", "status": "skipped"})

            events = self._drain_courseboard_capture(page)
            summary = _courseboard_runtime_capture_summary(events, base_url=config.base_url)
            if summary["endpoints"]:
                status = "ok"
                reason = None
            elif summary["event_count"] > 0:
                status = "no_match"
                reason = "Runtime instrumentation observed courseboard activity, but no read-oriented AJAX endpoint was confirmed."
            else:
                status = "no_match"
                reason = "No runtime courseboard XHR/fetch or form activity was observed during safe interactions."
            summary.update(
                {
                    "status": status,
                    "reason": reason,
                    "manual_capture_seconds": manual_seconds,
                    "board_url": board_url,
                    "actions": actions,
                    "dom_summary": dom_summary,
                }
            )
            return summary
        except Exception as exc:  # noqa: BLE001
            return {
                "status": "error",
                "board_url": abs_url(config.base_url, f"/mod/courseboard/view.php?id={board_ids[0]}"),
                "actions": actions,
                "error": str(exc),
                "endpoints": [],
            }
        finally:
            page.close()

    @staticmethod
    def _selector_href(page: Any, selector: str) -> str | None:
        try:
            href = page.eval_on_selector(selector, "el => el.href || el.getAttribute('href') || ''")
        except Exception:
            return None
        text = str(href or "").strip()
        return text or None

    @staticmethod
    def _click_selector(page: Any, selector: str, *, wait_for_navigation: bool, settle_ms: int) -> bool:
        try:
            locator = page.locator(selector).first
            if locator.count() == 0:
                return False
            if wait_for_navigation:
                locator.click(timeout=5_000)
                page.wait_for_load_state("domcontentloaded", timeout=30_000)
            else:
                locator.click(timeout=5_000)
            page.wait_for_timeout(max(0, settle_ms))
            return True
        except Exception:
            return False

    @staticmethod
    def _drain_courseboard_capture(page: Any) -> list[dict[str, Any]]:
        try:
            raw = page.evaluate(
                "() => (window.__kaistCourseboardCapture && window.__kaistCourseboardCapture.drain ? window.__kaistCourseboardCapture.drain() : [])"
            )
        except Exception:
            return []
        if not isinstance(raw, list):
            return []
        return [dict(item) for item in raw if isinstance(item, dict)]

    @staticmethod
    def _courseboard_dom_summary(page: Any) -> dict[str, Any]:
        try:
            payload = page.evaluate(
                """
                () => ({
                  article_link_count: document.querySelectorAll('a[href*="/mod/courseboard/article.php"]').length,
                  pagination_link_count: document.querySelectorAll('a[href*="/mod/courseboard/view.php"][href*="page="]').length,
                  comment_info_trigger_count: document.querySelectorAll('[onclick*="comment_info"], [href*="comment_info"], [data-type="comment_info"]').length,
                  form_count: document.querySelectorAll('form[action*="/mod/courseboard/"]').length
                })
                """
            )
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}

    def _probe_course_contents(self, *, context: Any, config: KlmsConfig, sesskey: str, course_ids: list[str]) -> dict[str, Any]:
        if not course_ids:
            return {
                "probe": "core_course_get_contents",
                "status": "skipped",
                "reason": "No course IDs available for probing.",
            }

        page = context.new_page()
        try:
            page.goto(abs_url(config.base_url, config.dashboard_path), wait_until="domcontentloaded", timeout=30_000)
            methodname = "core_course_get_contents"
            ajax_url = abs_url(
                config.base_url,
                f"/lib/ajax/service.php?sesskey={quote(sesskey)}&info={quote(methodname)}",
            )
            for course_id in course_ids[:3]:
                payload = [{"index": 0, "methodname": methodname, "args": {"courseid": int(course_id)}}]
                result = page.evaluate(
                    """
                    async ({url, payload}) => {
                      const response = await fetch(url, {
                        method: "POST",
                        headers: {
                          "Content-Type": "application/json",
                          "X-Requested-With": "XMLHttpRequest",
                          "Accept": "application/json, text/javascript, */*; q=0.01"
                        },
                        body: JSON.stringify(payload),
                        credentials: "same-origin"
                      });
                      const text = await response.text();
                      return {
                        ok: response.ok,
                        status: response.status,
                        url: response.url,
                        contentType: response.headers.get("content-type") || "",
                        text
                      };
                    }
                    """,
                    {"url": ajax_url, "payload": payload},
                )
                data = _unwrap_moodle_ajax_data(str(result.get("text") or ""))
                if not isinstance(data, list):
                    continue
                module_count = 0
                sample_module = None
                for section in data:
                    if not isinstance(section, dict):
                        continue
                    modules = section.get("modules")
                    if not isinstance(modules, list):
                        continue
                    module_count += len(modules)
                    if sample_module is None:
                        sample_module = next((module for module in modules if isinstance(module, dict)), None)
                return {
                    "probe": methodname,
                    "status": "ok",
                    "course_id": course_id,
                    "section_count": len(data),
                    "module_count": module_count,
                    "sample_module": {
                        "id": sample_module.get("id"),
                        "modname": sample_module.get("modname"),
                        "name": sample_module.get("name"),
                        "url": sample_module.get("url"),
                    }
                    if isinstance(sample_module, dict)
                    else None,
                    "content_type": result.get("contentType"),
                }
            return {
                "probe": methodname,
                "status": "no_match",
                "reason": "Endpoint responded but no usable course contents payload was extracted.",
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "probe": "core_course_get_contents",
                "status": "error",
                "error": str(exc),
            }
        finally:
            page.close()

    def _visit_page(
        self,
        *,
        context: Any,
        target: str,
        visited_urls: list[str],
        visit_errors: list[dict[str, str]],
    ) -> str | None:
        page = context.new_page()
        try:
            page.goto(target, wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_timeout(600)
            visited_urls.append(page.url)
            try:
                return page.content()
            except Exception:
                return None
        except Exception as exc:  # noqa: BLE001
            visit_errors.append({"target": target, "error": str(exc)})
            return None
        finally:
            page.close()
