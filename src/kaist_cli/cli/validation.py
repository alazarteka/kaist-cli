from __future__ import annotations

import argparse


def positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("must be an integer greater than 0") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return parsed


def positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("must be a number greater than 0") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return parsed


def nonnegative_int(value: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("must be an integer greater than or equal to 0") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be greater than or equal to 0")
    return parsed


def bounded_int(*, minimum: int, maximum: int):
    def parse(value: str) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise argparse.ArgumentTypeError(f"must be an integer from {minimum} to {maximum}") from exc
        if not minimum <= parsed <= maximum:
            raise argparse.ArgumentTypeError(f"must be from {minimum} to {maximum}")
        return parsed

    return parse


def bounded_float(*, minimum: float, maximum: float):
    def parse(value: str) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError) as exc:
            raise argparse.ArgumentTypeError(f"must be a number from {minimum:g} to {maximum:g}") from exc
        if not minimum <= parsed <= maximum:
            raise argparse.ArgumentTypeError(f"must be from {minimum:g} to {maximum:g}")
        return parsed

    return parse
