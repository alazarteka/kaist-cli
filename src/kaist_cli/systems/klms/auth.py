from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from ... import klms as legacy


@asynccontextmanager
async def klms_runtime(*, headless: bool = True, accept_downloads: bool = True) -> AsyncIterator[dict[str, Any]]:
    async with legacy.klms_runtime(headless=headless, accept_downloads=accept_downloads) as state:
        yield state


def login(base_url: str | None = None) -> dict[str, Any]:
    return legacy.klms_bootstrap_login(base_url)


async def status(*, validate: bool = True) -> dict[str, Any]:
    return await legacy.klms_status(validate=validate)


def refresh(base_url: str | None = None, *, validate: bool = True) -> dict[str, Any]:
    return legacy.klms_refresh_auth(base_url, validate=validate)


async def doctor(*, validate: bool = True) -> dict[str, Any]:
    return await legacy.klms_auth_doctor(validate=validate)
