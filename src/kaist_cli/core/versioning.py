from __future__ import annotations

import platform
import sys
from typing import Any

from .. import __version__
from .distribution import discover_distribution_info


def version_string() -> str:
    return __version__


def version_payload() -> dict[str, Any]:
    payload = {
        "version": version_string(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "executable": sys.executable,
        "frozen": bool(getattr(sys, "frozen", False)),
    }
    payload.update(discover_distribution_info().as_payload())
    return payload
