from __future__ import annotations

import json
from typing import Any

from ..contracts import CommandError, CommandResult
from .auth import AuthService, looks_logged_out_html, looks_login_url
from .config import abs_url, load_config
from .paths import KlmsPaths


class RequestService:
    def __init__(self, paths: KlmsPaths, auth: AuthService) -> None:
        self._paths = paths
        self._auth = auth

    def get(
        self,
        target: str,
        *,
        preview_chars: int = 4000,
        full_body: bool = False,
    ) -> CommandResult:
        config = load_config(self._paths)
        requested = str(target or "").strip()
        if not requested:
            raise CommandError(
                code="CONFIG_INVALID",
                message="Request target must not be empty.",
                hint="Pass a KLMS path like `/course/view.php?id=12345` or a full URL under the configured base URL.",
                exit_code=40,
                retryable=False,
            )
        base_url = config.base_url.rstrip("/")
        if requested.startswith(("http://", "https://")):
            if not requested.startswith(base_url):
                raise CommandError(
                    code="CONFIG_INVALID",
                    message="Authenticated KLMS requests must stay under the configured KLMS base URL.",
                    hint=f"Use a path under {base_url}, or update the saved base URL first.",
                    exit_code=40,
                    retryable=False,
                )
            target_url = requested
        else:
            target_url = abs_url(base_url, requested if requested.startswith("/") else f"/{requested}")
        preview_chars = max(200, min(int(preview_chars), 50_000))

        def callback(context: Any, auth_mode: str) -> CommandResult:
            page = context.new_page()
            try:
                result = page.evaluate(
                    """
                    async ({url}) => {
                      const response = await fetch(url, {
                        method: "GET",
                        credentials: "same-origin",
                        headers: { "Accept": "*/*" }
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
                    {"url": target_url},
                )
            finally:
                page.close()
            if not isinstance(result, dict):
                raise CommandError(
                    code="AUTH_FAILED",
                    message="KLMS request returned an invalid browser response payload.",
                    hint="Retry the request, or inspect the same URL in a browser-backed KLMS session.",
                    exit_code=10,
                    retryable=True,
                )
            final_url = str(result.get("url") or target_url)
            body_text = str(result.get("text") or "")
            if looks_login_url(final_url) or looks_logged_out_html(body_text):
                raise CommandError(
                    code="AUTH_EXPIRED",
                    message=f"Saved KLMS auth redirected the raw request to login ({final_url}).",
                    hint="Run `kaist klms auth refresh` and retry the request.",
                    exit_code=10,
                    retryable=True,
                )
            content_type = str(result.get("contentType") or "").strip() or None
            payload: dict[str, Any] = {
                "request_url": target_url,
                "final_url": final_url,
                "http_status": int(result.get("status") or 0),
                "ok": bool(result.get("ok")),
                "content_type": content_type,
                "auth_mode": auth_mode,
            }
            if full_body or len(body_text) <= preview_chars:
                payload["body_text"] = body_text
                payload["truncated"] = False
            else:
                payload["body_preview"] = body_text[:preview_chars]
                payload["body_length"] = len(body_text)
                payload["truncated"] = True
            if content_type and "json" in content_type.lower():
                try:
                    payload["body_json"] = json.loads(body_text)
                except Exception:
                    pass
            return CommandResult(data=payload, source="browser", capability="full")

        return self._auth.run_authenticated(
            config=config,
            headless=True,
            accept_downloads=False,
            timeout_seconds=15.0,
            callback=callback,
        )
