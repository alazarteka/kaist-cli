from __future__ import annotations

from .... import klms as legacy


CACHE_PATH = legacy.CACHE_PATH


def load_cache() -> dict[str, object]:
    return legacy._load_cache()  # type: ignore[attr-defined]
