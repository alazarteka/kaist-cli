from __future__ import annotations

import argparse
import sys
import traceback

from kaist_cli.cli.dispatch import dispatch
from kaist_cli.cli.errors import emit_json_error
from kaist_cli.cli.output import emit_human_output, emit_json
from kaist_cli.cli.parser import CliParseError, build_parser
from kaist_cli.core.envelope import success_envelope
from kaist_cli.core.error_registry import CliErrorDescriptor, classify_error
from kaist_cli.core.terminal import sanitize_terminal_text


def _agent_requested(argv: list[str]) -> bool:
    return "--agent" in argv


def main(argv: list[str] | None = None) -> int:
    args_list = list(argv) if argv is not None else sys.argv[1:]
    agent_mode = _agent_requested(args_list)
    parser = build_parser(agent_mode=agent_mode)
    try:
        args = parser.parse_args(args_list)
    except CliParseError as exc:
        if not agent_mode:
            raise
        parse_args = argparse.Namespace(schema_name="kaist.cli.parse.v1", command_path="parse")
        emit_json_error(
            parse_args,
            CliErrorDescriptor("CONFIG_INVALID", 2, False, "Run `kaist --help` to see valid commands and options."),
            sanitize_terminal_text(str(exc)),
            sort_keys=True,
        )
        return 2

    output_format = "json" if args.agent or getattr(args, "json", False) else args.format
    json_mode = output_format == "json"

    try:
        result = dispatch(args)
        if json_mode:
            emit_json(success_envelope(args, result), sort_keys=args.agent)
        else:
            emit_human_output(result, output_format, command_path=getattr(args, "command_path", None))
        return 0
    except KeyboardInterrupt:
        if json_mode:
            descriptor = CliErrorDescriptor("INTERNAL", 130, True, "retry command")
            emit_json_error(args, descriptor, "Interrupted.", sort_keys=args.agent)
        else:
            print("Interrupted.", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001
        descriptor = classify_error(exc)
        if args.debug:
            traceback.print_exc(file=sys.stderr)
        if json_mode:
            emit_json_error(args, descriptor, str(exc), sort_keys=args.agent)
        else:
            print(f"error [{descriptor.code.lower()}]: {sanitize_terminal_text(exc)}", file=sys.stderr)
        return descriptor.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
