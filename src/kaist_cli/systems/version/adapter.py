from __future__ import annotations

import argparse
from typing import Any

from ...core.contracts import SystemAdapter
from ...core.versioning import version_payload


class VersionAdapter(SystemAdapter):
    system_name = "version"

    def register(self, top_subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
        version = top_subparsers.add_parser("version", help="Show kaist CLI version and runtime info")
        version.set_defaults(
            handler=self._handle_version,
            schema_name="kaist.cli.version.v1",
            command_path="version",
        )

    def _handle_version(self, args: argparse.Namespace) -> dict[str, Any]:  # noqa: ARG002
        return version_payload()
