from __future__ import annotations

import json
import re
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, build_opener, HTTPRedirectHandler

from ..contracts import CommandResult
from .auth import APP_LOGIN_PATHS, AuthService, extract_sesskey, looks_logged_out_html, looks_login_url
from .config import abs_url, maybe_load_config
from .discovery import load_json_summary, load_recent_courses_args
from .paths import KlmsPaths

DEFAULT_BROWSER_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Upgrade-Insecure-Requests": "1",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/134.0.0.0 Safari/537.36"
    ),
}


def _load_cookie_header(paths: KlmsPaths, target_url: str) -> str | None:
    if not paths.storage_state_path.exists():
        return None
    try:
        payload = json.loads(paths.storage_state_path.read_text(encoding="utf-8"))
    except Exception:
        return None

    parsed = urlparse(target_url)
    host = parsed.hostname or ""
    path = parsed.path or "/"
    now_epoch = time.time()

    cookie_parts: list[str] = []
    for cookie in payload.get("cookies") or []:
        if not isinstance(cookie, dict):
            continue
        name = str(cookie.get("name") or "").strip()
        value = str(cookie.get("value") or "").strip()
        domain = str(cookie.get("domain") or "").lstrip(".").lower()
        cookie_path = str(cookie.get("path") or "/")
        expires = cookie.get("expires")
        if not name or not value:
            continue
        if domain and not (host == domain or host.endswith("." + domain)):
            continue
        if cookie_path and not path.startswith(cookie_path):
            continue
        if isinstance(expires, (int, float)) and float(expires) > 0 and float(expires) < now_epoch:
            continue
        cookie_parts.append(f"{name}={value}")

    if not cookie_parts:
        return None
    return "; ".join(cookie_parts)


def _http_request(
    *,
    method: str,
    url: str,
    timeout_seconds: float,
    preview_bytes: int = 2000,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
) -> dict[str, Any]:
    opener = build_opener(HTTPRedirectHandler())
    request = Request(url, data=body, method=method.upper())
    merged_headers = dict(DEFAULT_BROWSER_HEADERS)
    merged_headers.update(headers or {})
    for key, value in merged_headers.items():
        request.add_header(key, value)
    try:
        with opener.open(request, timeout=timeout_seconds) as response:
            raw = response.read(preview_bytes)
            final_url = response.geturl()
            content_type = str(response.headers.get("content-type") or "")
            text = raw.decode("utf-8", errors="replace")
            return {
                "ok": 200 <= int(response.status) < 300,
                "status": int(response.status),
                "final_url": final_url,
                "content_type": content_type,
                "preview": text[:400],
                "login_url_detected": looks_login_url(final_url),
                "login_html_detected": looks_logged_out_html(text) if "html" in content_type.lower() else False,
            }
    except HTTPError as exc:
        raw = exc.read(preview_bytes)
        final_url = exc.geturl()
        content_type = str(exc.headers.get("content-type") or "")
        text = raw.decode("utf-8", errors="replace")
        return {
            "ok": False,
            "status": int(exc.code),
            "final_url": final_url,
            "content_type": content_type,
            "preview": text[:400],
            "error": str(exc),
            "login_url_detected": looks_login_url(final_url),
            "login_html_detected": looks_logged_out_html(text) if "html" in content_type.lower() else False,
        }
    except URLError as exc:
        return {
            "ok": False,
            "status": None,
            "final_url": url,
            "content_type": "",
            "preview": "",
            "error": str(exc.reason),
            "login_url_detected": looks_login_url(url),
            "login_html_detected": False,
        }


class CapabilityProbeService:
    def __init__(self, paths: KlmsPaths, auth: AuthService) -> None:
        self._paths = paths
        self._auth = auth

    def plan(self) -> CommandResult:
        return CommandResult(
            data={
                "phase": "bootstrap",
                "branch": "codex/klms-v2",
                "provider_order": [
                    "moodle-standard",
                    "klms-ajax",
                    "html",
                    "browser-fallback",
                ],
                "next_moves": [
                    "add boundary guard against legacy imports",
                    "implement live capability validation on top of the offline probe",
                    "ship courses as the first real vertical slice",
                    "build today and inbox on top of canonical course/notice/assignment models",
                ],
            },
            source="probe",
            capability="partial",
        )

    def _live_validation(self, *, config: Any, timeout_seconds: float) -> dict[str, Any]:
        base_url = str(config.base_url)
        dashboard_path = str(config.dashboard_path)
        checks: list[dict[str, Any]] = []

        def add_check(
            *,
            label: str,
            provider: str,
            path: str,
            auth_required: bool,
            method: str = "GET",
            headers: dict[str, str] | None = None,
            body: bytes | None = None,
        ) -> dict[str, Any]:
            url = abs_url(base_url, path)
            request_headers = dict(headers or {})
            if auth_required:
                cookie_header = _load_cookie_header(self._paths, url)
                if not cookie_header:
                    result = {
                        "label": label,
                        "provider": provider,
                        "path": path,
                        "method": method,
                        "auth_required": True,
                        "status": "skipped",
                        "reason": "No storage_state cookies available for authenticated probe.",
                        "url": url,
                    }
                    checks.append(result)
                    return result
                request_headers["Cookie"] = cookie_header
            response = _http_request(
                method=method,
                url=url,
                timeout_seconds=timeout_seconds,
                headers=request_headers,
                body=body,
            )
            result = {
                "label": label,
                "provider": provider,
                "path": path,
                "method": method,
                "auth_required": auth_required,
                "url": url,
                **response,
            }
            checks.append(result)
            return result

        add_check(
            label="moodle_mobile_launch",
            provider="moodle-standard",
            path="/admin/tool/mobile/launch.php",
            auth_required=False,
        )
        add_check(
            label="moodle_login_token",
            provider="moodle-standard",
            path="/login/token.php",
            auth_required=False,
        )
        add_check(
            label="moodle_rest_server",
            provider="moodle-standard",
            path="/webservice/rest/server.php",
            auth_required=False,
        )
        for app_path in APP_LOGIN_PATHS:
            add_check(
                label=f"custom_login:{app_path}",
                provider="browser-fallback",
                path=app_path,
                auth_required=False,
            )

        add_check(
            label="dashboard_html",
            provider="html",
            path=dashboard_path,
            auth_required=True,
        )

        browser_validation = self._auth.browser_probe(
            config=config,
            timeout_seconds=timeout_seconds,
            recent_courses_args=load_recent_courses_args(self._paths),
        )

        content_api_validation = self._browser_content_api_probe(
            config=config,
            timeout_seconds=timeout_seconds,
        )

        return {
            "enabled": True,
            "timeout_seconds": timeout_seconds,
            "checks": checks,
            "browser_validation": browser_validation,
            "browser_content_api_validation": content_api_validation,
        }

    def _browser_content_api_probe(self, *, config: Any, timeout_seconds: float) -> dict[str, Any]:
        def callback(context: Any, auth_mode: str) -> dict[str, Any]:
            page = context.new_page()
            try:
                page.goto(config.base_url.rstrip("/") + config.dashboard_path, wait_until="domcontentloaded", timeout=max(1_000, int(timeout_seconds * 1000)))
                html = page.content()
                sesskey = extract_sesskey(html)
                if not sesskey:
                    return {
                        "status": "skipped",
                        "auth_mode": auth_mode,
                        "reason": "Dashboard HTML did not expose sesskey.",
                    }
                course_ids = list(dict.fromkeys(re.findall(r"/course/view\\.php\\?id=(\\d+)", html)))
                if not course_ids:
                    return {
                        "status": "skipped",
                        "auth_mode": auth_mode,
                        "reason": "Dashboard HTML did not expose any course IDs.",
                    }
                course_id = course_ids[0]
                methodname = "core_course_get_contents"
                ajax_url = f"{config.base_url.rstrip('/')}/lib/ajax/service.php?sesskey={sesskey}&info={methodname}"
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
                report: dict[str, Any] = {
                    "status": "ok" if result.get("ok") else "error",
                    "auth_mode": auth_mode,
                    "course_id": course_id,
                    "http_status": result.get("status"),
                    "final_url": result.get("url"),
                    "content_type": result.get("contentType"),
                }
                text = str(result.get("text") or "")
                try:
                    parsed = json.loads(text)
                except Exception as exc:
                    report["json_parse_ok"] = False
                    report["parse_error"] = str(exc)
                    report["preview"] = text[:400]
                    return report
                report["json_parse_ok"] = True
                if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
                    first = parsed[0]
                    report["ajax_error"] = bool(first.get("error"))
                    data = first.get("data")
                    if isinstance(data, list):
                        report["section_count"] = len(data)
                        for section in data:
                            if not isinstance(section, dict):
                                continue
                            modules = section.get("modules")
                            if isinstance(modules, list):
                                report["module_count"] = len(modules)
                                sample_module = next((module for module in modules if isinstance(module, dict)), None)
                                if isinstance(sample_module, dict):
                                    report["sample_module"] = {
                                        "id": sample_module.get("id"),
                                        "modname": sample_module.get("modname"),
                                        "name": sample_module.get("name"),
                                        "url": sample_module.get("url"),
                                    }
                                break
                return report
            finally:
                page.close()

        try:
            return self._auth.run_authenticated(
                config=config,
                headless=True,
                accept_downloads=False,
                timeout_seconds=timeout_seconds,
                callback=callback,
            )
        except Exception as exc:  # noqa: BLE001
            return {
                "status": "error",
                "error": str(exc),
            }

    def probe(self, *, live: bool = False, timeout_seconds: float = 10.0) -> CommandResult:
        auth_snapshot = self._auth.snapshot()
        config = maybe_load_config(self._paths)
        api_map = load_json_summary(str(self._paths.api_map_path))
        endpoint_discovery = load_json_summary(str(self._paths.endpoint_discovery_path))

        provider_candidates: list[dict[str, Any]] = []
        base_url = config.base_url if config is not None else None
        dashboard_path = config.dashboard_path if config is not None else "/my/"

        def add_candidate(
            *,
            provider: str,
            kind: str,
            path: str,
            auth_required: bool,
            evidence: str,
        ) -> None:
            provider_candidates.append(
                {
                    "provider": provider,
                    "kind": kind,
                    "path": path,
                    "url": abs_url(base_url, path) if base_url else None,
                    "auth_required": auth_required,
                    "status": "candidate" if base_url else "needs_config",
                    "evidence": evidence,
                }
            )

        add_candidate(
            provider="moodle-standard",
            kind="public_config",
            path="/webservice/rest/server.php",
            auth_required=False,
            evidence="Common Moodle mobile/web-service entrypoint.",
        )
        add_candidate(
            provider="moodle-standard",
            kind="mobile_launch",
            path="/admin/tool/mobile/launch.php",
            auth_required=False,
            evidence="Common Moodle mobile launch endpoint.",
        )
        add_candidate(
            provider="moodle-standard",
            kind="login_token",
            path="/login/token.php",
            auth_required=False,
            evidence="Common Moodle token endpoint.",
        )
        add_candidate(
            provider="klms-ajax",
            kind="ajax_service",
            path="/lib/ajax/service.php",
            auth_required=True,
            evidence="Current CLI already relies on Moodle AJAX patterns.",
        )
        add_candidate(
            provider="moodle-standard",
            kind="course_contents",
            path="/lib/ajax/service.php",
            auth_required=True,
            evidence="Likely Moodle course content/files read API via core_course_get_contents.",
        )
        add_candidate(
            provider="html",
            kind="dashboard",
            path=dashboard_path,
            auth_required=True,
            evidence="Known HTML fallback path.",
        )
        add_candidate(
            provider="klms-ajax",
            kind="courseboard_read_hint",
            path="/mod/courseboard/ajax.php",
            auth_required=True,
            evidence="Courseboard JS hints expose read-like comment_info lookups without using mutating action endpoints.",
        )
        for app_path in APP_LOGIN_PATHS:
            add_candidate(
                provider="browser-fallback",
                kind="custom_login",
                path=app_path,
                auth_required=False,
                evidence="Observed inside the KLMS Android app bundle.",
            )

        recommended_provider_order = [
            "moodle-standard",
            "klms-ajax",
            "html",
            "browser-fallback",
        ]
        recommended_endpoints = []
        if isinstance(api_map, dict):
            for endpoint in api_map.get("recommended_endpoints") or []:
                if not isinstance(endpoint, dict):
                    continue
                recommended_endpoints.append(
                    {
                        "category": endpoint.get("category"),
                        "confidence": endpoint.get("confidence"),
                        "canonical_key": endpoint.get("canonical_key"),
                        "url": endpoint.get("url"),
                    }
                )
            recommended_endpoints = recommended_endpoints[:8]

        capability = "partial" if base_url else "planned"
        report = {
            "configured": config is not None,
            "config": auth_snapshot.get("config"),
            "auth_mode": auth_snapshot.get("auth_mode"),
            "validation_mode": "live" if live else auth_snapshot.get("validation_mode"),
            "recommended_provider_order": recommended_provider_order,
            "provider_candidates": provider_candidates,
            "discovery_artifacts": {
                "endpoint_discovery_exists": self._paths.endpoint_discovery_path.exists(),
                "endpoint_discovery_path": str(self._paths.endpoint_discovery_path),
                "endpoint_count": int(endpoint_discovery.get("endpoint_count", 0)) if endpoint_discovery else 0,
                "api_map_exists": self._paths.api_map_path.exists(),
                "api_map_path": str(self._paths.api_map_path),
                "recommended_endpoints": recommended_endpoints,
            },
            "login_flow_evidence": {
                "android_app_paths": list(APP_LOGIN_PATHS),
                "auth_mode": auth_snapshot.get("auth_mode"),
                "recommended_action": auth_snapshot.get("recommended_action"),
            },
        }
        if live and config is not None:
            report["live_validation"] = self._live_validation(
                config=config,
                timeout_seconds=max(1.0, float(timeout_seconds)),
            )
        elif live:
            report["live_validation"] = {
                "enabled": True,
                "checks": [],
                "status": "skipped",
                "reason": "KLMS config is required for live validation.",
            }
        return CommandResult(data=report, source="probe", capability=capability)
