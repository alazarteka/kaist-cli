from __future__ import annotations

import argparse

from ..core.distribution import discover_distribution_info
from ..core.system_registry import default_registry
from ..core.help_format import HelpFormatter, dedent as _dedent
from ..core.versioning import version_string


class CliParseError(ValueError):
    """An argparse validation error that can be rendered in agent mode."""


class _CliArgumentParser(argparse.ArgumentParser):
    def __init__(self, *args, agent_mode: bool = False, **kwargs):
        self.agent_mode = agent_mode
        super().__init__(*args, **kwargs)

    def add_subparsers(self, **kwargs):  # type: ignore[no-untyped-def]
        kwargs.setdefault(
            "parser_class",
            lambda *args, **parser_kwargs: type(self)(*args, agent_mode=self.agent_mode, **parser_kwargs),
        )
        return super().add_subparsers(**kwargs)

    def error(self, message: str) -> None:
        if self.agent_mode:
            raise CliParseError(message)
        super().error(message)


def build_parser(*, agent_mode: bool = False) -> argparse.ArgumentParser:
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
    parser = _CliArgumentParser(
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
        agent_mode=agent_mode,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {version_string()}", help="Show the CLI version and exit.")
    parser.add_argument("--debug", action="store_true", help="Print traceback on failures.")
    parser.add_argument(
        "--format",
        choices=["auto", "json", "table", "text"],
        default="auto",
        help="Output format. auto selects table/text in TTY and json in non-TTY.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON envelopes (alias for --format json).",
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
