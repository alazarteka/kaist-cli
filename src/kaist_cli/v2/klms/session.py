from __future__ import annotations

import http.cookiejar
import ssl
import urllib.request
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Any

import certifi

from ..contracts import CommandError
from .auth import extract_sesskey, looks_logged_out_html, looks_login_url
from .config import KlmsConfig, abs_url
from .deadline import RefreshDeadline
from .paths import KlmsPaths

DEFAULT_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
}


@dataclass(frozen=True)
class KlmsHttpResponse:
    url: str
    text: str
    via: str


@dataclass(frozen=True)
class KlmsSessionBootstrap:
    config: KlmsConfig
    auth_mode: str
    dashboard_url: str
    dashboard_html: str
    dashboard_sesskey: str | None
    http: "KlmsHttpSession"


def _cookie_from_state_row(row: dict[str, Any]) -> http.cookiejar.Cookie | None:
    name = str(row.get("name") or "").strip()
    value = str(row.get("value") or "")
    domain = str(row.get("domain") or "").strip()
    path = str(row.get("path") or "/").strip() or "/"
    if not name or not domain:
        return None
    expires = row.get("expires")
    expires_value = int(float(expires)) if isinstance(expires, (int, float)) and float(expires) > 0 else None
    secure = bool(row.get("secure"))
    http_only = bool(row.get("httpOnly"))
    return http.cookiejar.Cookie(
        version=0,
        name=name,
        value=value,
        port=None,
        port_specified=False,
        domain=domain,
        domain_specified=True,
        domain_initial_dot=domain.startswith("."),
        path=path,
        path_specified=True,
        secure=secure,
        expires=expires_value,
        discard=expires_value is None,
        comment=None,
        comment_url=None,
        rest={"HttpOnly": http_only},
        rfc2109=False,
    )


class KlmsHttpSession:
    def __init__(self, context: Any, *, base_url: str) -> None:
        self._base_url = base_url.rstrip("/")
        state = context.storage_state()
        cookies = state.get("cookies") if isinstance(state, dict) else []
        self._cookie_rows = [dict(row) for row in cookies if isinstance(row, dict)]

    def _build_opener(self) -> urllib.request.OpenerDirector:
        cookie_jar = http.cookiejar.CookieJar()
        for row in self._cookie_rows:
            cookie = _cookie_from_state_row(row)
            if cookie is not None:
                cookie_jar.set_cookie(cookie)

        ssl_context = ssl.create_default_context(cafile=certifi.where())
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(cookie_jar),
            urllib.request.HTTPSHandler(context=ssl_context),
        )
        opener.addheaders = list(DEFAULT_HEADERS.items())
        return opener

    def get_html(
        self,
        url_or_path: str,
        *,
        context: Any | None = None,
        timeout_seconds: float = 20.0,
    ) -> KlmsHttpResponse:
        target_url = abs_url(self._base_url, url_or_path)
        try:
            opener = self._build_opener()
            request = urllib.request.Request(target_url, headers=DEFAULT_HEADERS)
            with opener.open(request, timeout=max(1.0, timeout_seconds)) as response:
                raw = response.read()
                charset = response.headers.get_content_charset() or "utf-8"
                try:
                    text = raw.decode(charset, errors="replace")
                except LookupError:
                    text = raw.decode("utf-8", errors="replace")
                result = KlmsHttpResponse(
                    url=str(response.geturl() or target_url),
                    text=text,
                    via="http",
                )
        except Exception:
            if context is None:
                raise
            return self._browser_fallback(context, target_url, timeout_seconds=timeout_seconds)

        if context is not None and (looks_login_url(result.url) or looks_logged_out_html(result.text)):
            return self._browser_fallback(context, target_url, timeout_seconds=timeout_seconds)
        return result

    def post_text(
        self,
        url_or_path: str,
        *,
        body: str,
        headers: dict[str, str] | None = None,
        timeout_seconds: float = 20.0,
    ) -> KlmsHttpResponse:
        target_url = abs_url(self._base_url, url_or_path)
        merged_headers = dict(DEFAULT_HEADERS)
        if headers:
            merged_headers.update(headers)
        opener = self._build_opener()
        request = urllib.request.Request(
            target_url,
            data=body.encode("utf-8"),
            headers=merged_headers,
            method="POST",
        )
        with opener.open(request, timeout=max(1.0, timeout_seconds)) as response:
            raw = response.read()
            charset = response.headers.get_content_charset() or "utf-8"
            try:
                text = raw.decode(charset, errors="replace")
            except LookupError:
                text = raw.decode("utf-8", errors="replace")
            return KlmsHttpResponse(
                url=str(response.geturl() or target_url),
                text=text,
                via="http",
            )

    @staticmethod
    def _browser_fallback(context: Any, target_url: str, *, timeout_seconds: float) -> KlmsHttpResponse:
        page = context.new_page()
        try:
            page.goto(target_url, wait_until="domcontentloaded", timeout=max(1_000, int(timeout_seconds * 1000)))
            return KlmsHttpResponse(url=page.url, text=page.content(), via="browser")
        finally:
            page.close()


def fetch_html_batch(
    http: KlmsHttpSession,
    paths: list[str],
    *,
    deadline: RefreshDeadline | None = None,
    max_workers: int = 4,
) -> dict[str, KlmsHttpResponse]:
    ordered = [str(path).strip() for path in paths if str(path).strip()]
    if not ordered:
        return {}

    def worker(path: str) -> KlmsHttpResponse:
        timeout_seconds = deadline.request_timeout(20.0, use_soft=False) if deadline is not None else 20.0
        return http.get_html(path, timeout_seconds=timeout_seconds)

    results: dict[str, KlmsHttpResponse] = {}
    executor = ThreadPoolExecutor(max_workers=max(1, min(int(max_workers), len(ordered))))
    timed_out = False
    try:
        futures = {executor.submit(worker, path): path for path in ordered}
        timeout_seconds = deadline.remaining_hard() if deadline is not None else None
        done, not_done = wait(set(futures.keys()), timeout=timeout_seconds)
        for future in done:
            path = futures[future]
            results[path] = future.result()
        if not_done:
            timed_out = True
            for future in not_done:
                future.cancel()
            raise TimeoutError("Interactive refresh budget expired while waiting for batched HTTP fetches.")
    finally:
        executor.shutdown(wait=not timed_out, cancel_futures=timed_out)
    return results


def build_session_bootstrap(
    paths: KlmsPaths,
    *,
    context: Any,
    config: KlmsConfig,
    auth_mode: str,
    timeout_seconds: float = 20.0,
    dashboard_url: str | None = None,
    dashboard_html: str | None = None,
) -> KlmsSessionBootstrap:
    http = KlmsHttpSession(context, base_url=config.base_url)
    resolved_dashboard_url = dashboard_url
    resolved_dashboard_html = dashboard_html
    if resolved_dashboard_url is None or resolved_dashboard_html is None:
        dashboard = http.get_html(config.dashboard_path, context=context, timeout_seconds=timeout_seconds)
        resolved_dashboard_url = dashboard.url
        resolved_dashboard_html = dashboard.text
    if looks_login_url(str(resolved_dashboard_url or "")) or looks_logged_out_html(str(resolved_dashboard_html or "")):
        raise CommandError(
            code="AUTH_EXPIRED",
            message="Saved KLMS auth did not reach an authenticated dashboard session.",
            hint="Run `kaist klms auth refresh` and complete the login flow again.",
            exit_code=10,
            retryable=True,
        )
    return KlmsSessionBootstrap(
        config=config,
        auth_mode=auth_mode,
        dashboard_url=str(resolved_dashboard_url),
        dashboard_html=str(resolved_dashboard_html),
        dashboard_sesskey=extract_sesskey(str(resolved_dashboard_html)),
        http=http,
    )
