from __future__ import annotations

import argparse
import textwrap

from ..core.distribution import discover_distribution_info
from ..core.system_registry import default_registry


class HelpFormatter(argparse.ArgumentDefaultsHelpFormatter, argparse.RawDescriptionHelpFormatter):
    pass


def _dedent(text: str) -> str:
    return textwrap.dedent(text).strip()


def build_parser() -> argparse.ArgumentParser:
    distribution = discover_distribution_info()
    epilog = None
    if distribution.bundled_skill_path is not None:
        epilog = _dedent(
            f"""
            Bundled agent skill:
              Skill name: kaist-cli
              {distribution.bundled_skill_path}
              Codex install: kaist agent install codex
              Agents can install this skill directly from that path or use the install helper above.
            """
        )
    parser = argparse.ArgumentParser(
        prog="kaist",
        description=_dedent(
            """
            CLI for KAIST systems.

            KLMS quick start:
              1) kaist klms auth login --base-url https://klms.kaist.ac.kr
              2) kaist klms courses resolve "Operating Systems"
              3) kaist klms week

            Agent quick start:
              1) kaist agent install codex
              2) Use the kaist-cli skill for KLMS tasks
              3) Prefer `kaist --agent ...` commands in agent workflows

            Global machine mode:
              kaist --agent klms week
              kaist --agent version
            """
        ),
        epilog=epilog,
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
        help="Global agent mode. Forces strict JSON envelopes and deterministic key ordering; use it before the command, e.g. `kaist --agent klms today`.",
    )

    top = parser.add_subparsers(dest="system", required=True, title="Commands", metavar="COMMAND")
    registry = default_registry()
    registry.register_parsers(top)
    return parser
