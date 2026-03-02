from __future__ import annotations

import argparse
import textwrap
from typing import Any

from ...core.contracts import SystemAdapter
from . import auth, services


class _HelpFormatter(argparse.ArgumentDefaultsHelpFormatter, argparse.RawDescriptionHelpFormatter):
    pass


def _dedent(text: str) -> str:
    return textwrap.dedent(text).strip()


class PortalAdapter(SystemAdapter):
    system_name = "portal"

    def register(self, top_subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
        portal = top_subparsers.add_parser(
            "portal",
            help="KAIST school portal (scaffold)",
            description=_dedent(
                """
                Portal adapter scaffold.

                Shared auth/runtime boundaries are wired, but portal API/scraping
                implementations are intentionally deferred.
                """
            ),
            formatter_class=_HelpFormatter,
        )
        portal_sub = portal.add_subparsers(dest="group", required=True, title="Portal Commands", metavar="COMMAND")

        auth_parser = portal_sub.add_parser("auth", help="Portal auth scaffold", formatter_class=_HelpFormatter)
        auth_sub = auth_parser.add_subparsers(dest="action", required=True, title="Auth Commands", metavar="ACTION")

        auth_status = auth_sub.add_parser("status", help="Show portal scaffold status", formatter_class=_HelpFormatter)
        auth_status.set_defaults(
            handler=self._handle_auth_status,
            schema_name="kaist.portal.auth.status.v1",
            command_path="portal auth status",
        )

        auth_login = auth_sub.add_parser("login", help="Portal login scaffold", formatter_class=_HelpFormatter)
        auth_login.set_defaults(
            handler=self._handle_auth_login,
            schema_name="kaist.portal.auth.login.v1",
            command_path="portal auth login",
        )

        status = portal_sub.add_parser("status", help="Show portal capability scaffold", formatter_class=_HelpFormatter)
        status.set_defaults(
            handler=self._handle_status,
            schema_name="kaist.portal.status.v1",
            command_path="portal status",
        )

    def _handle_auth_status(self, args: argparse.Namespace) -> dict[str, Any]:  # noqa: ARG002
        return auth.status()

    def _handle_auth_login(self, args: argparse.Namespace) -> dict[str, Any]:  # noqa: ARG002
        return auth.login()

    def _handle_status(self, args: argparse.Namespace) -> dict[str, Any]:  # noqa: ARG002
        return services.capability_report()
