from __future__ import annotations

from typing import Any


def status() -> dict[str, Any]:
    return {
        "ok": True,
        "implemented": False,
        "system": "portal",
        "message": "Portal integration scaffold is in place. Data/auth flows are not implemented yet.",
    }


def login() -> dict[str, Any]:
    return {
        "ok": False,
        "implemented": False,
        "system": "portal",
        "message": "Portal login flow is not implemented yet.",
    }
