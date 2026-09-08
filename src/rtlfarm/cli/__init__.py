"""The ``rtlfarm`` command: a thin API client plus developer commands.

Each verb lives in its own module and registers itself on the parser; this
module holds only the global options and the dispatch. ``control run`` and
``admin migrate`` operate the control plane; ``dev pack``, ``dev pin-toolchain``
and ``toolchain manifest`` need no control plane at all.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable

from rtlfarm.cli import admin, control, dev, toolchain

Handler = Callable[[argparse.Namespace], int]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rtlfarm",
        description="rtlfarm: a small hardware regression farm.",
    )
    parser.add_argument(
        "--url", help="control-plane URL (default: the configured control_url)"
    )
    parser.add_argument("--token", help="bearer token for the control plane")
    parser.add_argument(
        "--json", action="store_true", help="print machine-readable JSON"
    )
    verbs = parser.add_subparsers(dest="verb", metavar="VERB")
    dev.register(verbs)
    toolchain.register(verbs)
    control.register(verbs)
    admin.register(verbs)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns the process exit code (2 is a usage error)."""
    parser = build_parser()
    args = parser.parse_args(argv)
    handler: Handler | None = getattr(args, "handler", None)
    if handler is None:
        parser.print_help(sys.stderr)
        return 2
    return handler(args)
