from __future__ import annotations

import json
from pathlib import Path
from typing import Any

def _legacy() -> Any:
    from .... import klms as legacy

    return legacy


async def run(*, update: bool = True, max_notice_pages: int = 3) -> dict[str, object]:
    return await _legacy().klms_sync_snapshot(update=update, max_notice_pages=max_notice_pages)


def status() -> dict[str, object]:
    snapshot_path = Path(_legacy().SNAPSHOT_PATH)
    if not snapshot_path.exists():
        return {
            "ok": True,
            "snapshot_exists": False,
            "snapshot_path": str(snapshot_path),
            "last_sync_iso": None,
        }
    try:
        payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"Failed to parse snapshot file {snapshot_path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Invalid snapshot format in {snapshot_path}")
    return {
        "ok": True,
        "snapshot_exists": True,
        "snapshot_path": str(snapshot_path),
        "last_sync_iso": payload.get("last_sync_iso"),
        "courses_count": len((payload.get("courses") or {}).keys()) if isinstance(payload.get("courses"), dict) else 0,
        "boards_count": len((payload.get("boards") or {}).keys()) if isinstance(payload.get("boards"), dict) else 0,
    }


def reset() -> dict[str, object]:
    snapshot_path = Path(_legacy().SNAPSHOT_PATH)
    existed = snapshot_path.exists()
    if existed:
        snapshot_path.unlink()
    return {
        "ok": True,
        "snapshot_path": str(snapshot_path),
        "removed": existed,
    }
