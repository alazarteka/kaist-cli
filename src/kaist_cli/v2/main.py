from __future__ import annotations

from typing import Sequence

from .contracts import CommandError
from .envelope import emit_json, emit_text, error_envelope, success_envelope
from .klms.commands import dispatch as dispatch_klms
from .klms.container import build_container
from .parser import build_parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        if args.system != "klms":
            raise CommandError(code="NOT_FOUND", message=f"Unknown system: {args.system}", exit_code=50)

        result = dispatch_klms(args, build_container())
        if args.json:
            emit_json(success_envelope(args, result))
        else:
            emit_text(result)
        return 0
    except CommandError as error:
        if args.json:
            emit_json(error_envelope(args, error))
        else:
            print(f"error [{error.code.lower()}]: {error.message}")
            if error.hint:
                print(f"hint: {error.hint}")
        return error.exit_code

