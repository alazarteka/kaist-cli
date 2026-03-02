from __future__ import annotations

from typing import Any


def _legacy() -> Any:
    from .... import klms as legacy

    return legacy


def is_video_filename(name: str) -> bool:
    return _legacy()._is_video_filename(name)  # type: ignore[attr-defined]


def is_video_url(url: str) -> bool:
    return _legacy()._is_video_url(url)  # type: ignore[attr-defined]


def material_kind_from_module(module: str | None) -> str:
    return _legacy()._material_kind_from_module(module)  # type: ignore[attr-defined]
