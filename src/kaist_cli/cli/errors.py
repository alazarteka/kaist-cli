from __future__ import annotations

import argparse

from ..core.envelope import error_envelope
from ..core.error_registry import CliErrorDescriptor
from .output import emit_json


def emit_json_error(args: argparse.Namespace, descriptor: CliErrorDescriptor, message: str, *, sort_keys: bool) -> None:
    emit_json(
        error_envelope(
            args,
            code=descriptor.code,
            message=message,
            retryable=descriptor.retryable,
            hint=descriptor.hint,
        ),
        sort_keys=sort_keys,
    )
