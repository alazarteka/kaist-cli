from __future__ import annotations

import argparse

from ...cli.help_format import HelpFormatter as _HelpFormatter
from ...cli.help_format import dedent as _dedent
from ...core.contracts import SystemAdapter
from ...v2.parser import register_klms_parser


class KlmsAdapter(SystemAdapter):
    system_name = "klms"

    def register(self, top_subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
        klms_parser = top_subparsers.add_parser(
            "klms",
            help="KAIST Learning Management System",
            description=_dedent(
                """
                Task-first KLMS interface.

                Core workflows:
                  auth, today, inbox, sync, courses, assignments, notices, files, videos

                Engineering/debug:
                  dev
                """
            ),
            formatter_class=_HelpFormatter,
        )
        register_klms_parser(klms_parser, schema_prefix="kaist.klms", handler=self._handle)

    @staticmethod
    def _handle(args: argparse.Namespace) -> object:
        from ...v2.klms.commands import dispatch as dispatch_v2
        from ...v2.klms.container import build_container

        result = dispatch_v2(args, build_container())
        setattr(args, "_explicit_source", result.source)
        setattr(args, "_explicit_capability", result.capability)
        return result.data
