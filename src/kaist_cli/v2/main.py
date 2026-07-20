"""Thin compatibility shim — prefer `python -m kaist_cli` / the `kaist` console script."""

from __future__ import annotations

from typing import Sequence

from kaist_cli.main import main as _main


def main(argv: Sequence[str] | None = None) -> int:
    return _main(list(argv) if argv is not None else None)
