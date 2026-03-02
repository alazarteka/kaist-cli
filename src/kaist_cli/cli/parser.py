from __future__ import annotations

import argparse
import textwrap

from ..core.system_registry import default_registry


class HelpFormatter(argparse.ArgumentDefaultsHelpFormatter, argparse.RawDescriptionHelpFormatter):
    pass


def _dedent(text: str) -> str:
    return textwrap.dedent(text).strip()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kaist",
        description=_dedent(
            """
            CLI for KAIST systems.

            KLMS quick start:
              1) kaist klms config set --base-url https://klms.kaist.ac.kr
              2) kaist klms auth login
              3) kaist klms list courses
            """
        ),
        formatter_class=HelpFormatter,
    )
    parser.add_argument("--debug", action="store_true", help="Print traceback on failures.")
    parser.add_argument(
        "--format",
        choices=["auto", "json", "table", "text"],
        default="auto",
        help="Output format. auto selects table/text in TTY and json in non-TTY.",
    )
    parser.add_argument(
        "--agent",
        action="store_true",
        help="Agent mode. Forces strict JSON envelopes and deterministic key ordering.",
    )

    top = parser.add_subparsers(dest="system", required=True, title="Commands", metavar="COMMAND")
    registry = default_registry()
    registry.register_parsers(top)
    return parser
