from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

def _legacy() -> Any:
    from ... import klms as legacy

    return legacy


@asynccontextmanager
async def klms_runtime(*, headless: bool = True, accept_downloads: bool = True) -> AsyncIterator[dict[str, Any]]:
    async with _legacy().klms_runtime(headless=headless, accept_downloads=accept_downloads) as state:
        yield state


def login(base_url: str | None = None) -> dict[str, Any]:
    return _legacy().klms_bootstrap_login(base_url)


def install_browser(*, force: bool = False) -> dict[str, Any]:
    return _legacy().klms_install_browser(force=force)


async def status(*, validate: bool = True) -> dict[str, Any]:
    return await _legacy().klms_status(validate=validate)


def refresh(base_url: str | None = None, *, validate: bool = True) -> dict[str, Any]:
    return _legacy().klms_refresh_auth(base_url, validate=validate)


async def doctor(*, validate: bool = True) -> dict[str, Any]:
    return await _legacy().klms_auth_doctor(validate=validate)
