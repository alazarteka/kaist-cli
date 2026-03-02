from __future__ import annotations

from typing import Any


def _legacy() -> Any:
    from .... import klms as legacy

    return legacy


async def get_file(
    url: str,
    *,
    filename: str | None = None,
    subdir: str | None = None,
    if_exists: str = "skip",
) -> dict[str, object]:
    return await _legacy().klms_download_file(url, filename=filename, subdir=subdir, if_exists=if_exists)
