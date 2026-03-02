from __future__ import annotations

from .... import klms as legacy


def is_video_filename(name: str) -> bool:
    return legacy._is_video_filename(name)  # type: ignore[attr-defined]


def is_video_url(url: str) -> bool:
    return legacy._is_video_url(url)  # type: ignore[attr-defined]


def material_kind_from_module(module: str | None) -> str:
    return legacy._material_kind_from_module(module)  # type: ignore[attr-defined]
