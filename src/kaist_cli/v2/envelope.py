from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from typing import Any

from .contracts import CommandError, CommandResult


def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def success_envelope(args: argparse.Namespace, result: CommandResult) -> dict[str, Any]:
    return {
        "schema": args.schema_name,
        "ok": True,
        "generated_at": utc_now_iso(),
        "meta": {
            "command": args.command_path,
            "source": result.source,
            "capability": result.capability,
        },
        "data": result.data,
    }


def error_envelope(args: argparse.Namespace, error: CommandError) -> dict[str, Any]:
    return {
        "schema": args.schema_name,
        "ok": False,
        "generated_at": utc_now_iso(),
        "error": {
            "code": error.code,
            "message": error.message,
            "retryable": error.retryable,
            "hint": error.hint,
        },
    }


def emit_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True))


def emit_text(result: CommandResult) -> None:
    data = result.data
    if isinstance(data, dict):
        for key, value in data.items():
            if isinstance(value, (dict, list)):
                print(f"{key}: {json.dumps(value, ensure_ascii=False)}")
            else:
                print(f"{key}: {value}")
        return

    if isinstance(data, list):
        if not data:
            print("(empty)")
            return
        for idx, item in enumerate(data, start=1):
            print(f"{idx}. {item}")
        return

    print(str(data))

