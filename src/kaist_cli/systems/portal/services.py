from __future__ import annotations

from typing import Any


def capability_report() -> dict[str, Any]:
    return {
        "ok": True,
        "implemented": False,
        "system": "portal",
        "capabilities": [],
    }
