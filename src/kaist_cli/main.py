from __future__ import annotations

import sys
import traceback

from .cli.dispatch import dispatch
from .cli.errors import emit_json_error
from .cli.output import emit_human_output, emit_json
from .cli.parser import build_parser
from .core.envelope import success_envelope
from .core.error_registry import CliErrorDescriptor, classify_error


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    output_format = "json" if args.agent else args.format
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
            print(f"error [{descriptor.code.lower()}]: {exc}", file=sys.stderr)
        return descriptor.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
