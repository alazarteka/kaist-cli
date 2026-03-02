from __future__ import annotations

import argparse
from typing import Any, Callable


def dispatch(args: argparse.Namespace) -> Any:
    handler = getattr(args, "handler", None)
    if not callable(handler):
        raise ValueError(f"No command handler registered for parsed args: {args}")
    func = handler
    return func(args)
