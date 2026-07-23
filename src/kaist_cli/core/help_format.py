from __future__ import annotations

import argparse
import textwrap


class HelpFormatter(argparse.ArgumentDefaultsHelpFormatter, argparse.RawDescriptionHelpFormatter):
    pass


def dedent(text: str) -> str:
    return textwrap.dedent(text).strip()
