"""Compatibility re-export; prefer ``kaist_cli.core.help_format``."""

from __future__ import annotations

from ..core.help_format import HelpFormatter, dedent

__all__ = ["HelpFormatter", "dedent"]
