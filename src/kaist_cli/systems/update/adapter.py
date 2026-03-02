from __future__ import annotations

import argparse
import textwrap
from typing import Any

from ...core.contracts import SystemAdapter
from ...core.updater import check_for_update, perform_self_update


class _HelpFormatter(argparse.ArgumentDefaultsHelpFormatter, argparse.RawDescriptionHelpFormatter):
    pass


def _dedent(text: str) -> str:
    return textwrap.dedent(text).strip()


class UpdateAdapter(SystemAdapter):
    system_name = "update"

    def register(self, top_subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
        update = top_subparsers.add_parser(
            "update",
            help="Check and install latest GitHub release binary",
            description=_dedent(
                """
                Self-update command for standalone kaist binaries.

                For source-based runs (uv/pip editable), this command supports
                --check but installation requires a standalone binary.

                Release source is fixed to: alazarteka/kaist-cli
                """
            ),
            formatter_class=_HelpFormatter,
        )
        update.add_argument("--check", action="store_true", help="Only check whether an update is available.")
        update.set_defaults(
            handler=self._handle_update,
            schema_name="kaist.cli.update.v1",
            command_path="update",
        )

    def _handle_update(self, args: argparse.Namespace) -> dict[str, Any]:
        if args.check:
            return check_for_update()
        return perform_self_update()
