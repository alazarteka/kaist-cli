from __future__ import annotations

import argparse
from typing import Any

from ...core.contracts import SystemAdapter
from ...core.versioning import version_payload


class VersionAdapter(SystemAdapter):
    system_name = "version"

    def register(self, top_subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
        version = top_subparsers.add_parser("version", help="Show kaist CLI version and runtime info")
        version.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="Emit the version envelope as JSON (also accepted before the command).")
        version.add_argument(
            "--format",
            choices=["auto", "json", "table", "text"],
            default=argparse.SUPPRESS,
            help="Select human or JSON output (also accepted before the command).",
        )
        version.set_defaults(
            handler=self._handle_version,
            schema_name="kaist.cli.version.v1",
            command_path="version",
        )

    def _handle_version(self, args: argparse.Namespace) -> dict[str, Any]:  # noqa: ARG002
        return version_payload()
