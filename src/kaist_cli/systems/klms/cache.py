from __future__ import annotations

import os
from pathlib import Path
from typing import Any


def _cache_path() -> Path:
    root = Path(os.environ.get("KAIST_CLI_HOME") or str(Path.home() / ".kaist-cli")).expanduser()
    return root / "private" / "klms" / "cache.json"


def _legacy() -> Any:
    from .... import klms as legacy

    return legacy


CACHE_PATH = _cache_path()


def load_cache() -> dict[str, object]:
    return _legacy()._load_cache()  # type: ignore[attr-defined]
