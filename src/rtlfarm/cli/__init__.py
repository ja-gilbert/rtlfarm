"""The ``rtlfarm`` command: a thin API client plus developer commands.

Each verb arrives together with the behaviour it drives. So far: the global
options and ``rtlfarm dev pack``, which packs a design directory and prints
its manifest without needing a control plane.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path

from rtlfarm.expand.pack import PackError, pack
from rtlfarm.expand.pipeline import PipelineError

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

    dev = verbs.add_parser("dev", help="developer commands; no control plane needed")
    dev_verbs = dev.add_subparsers(dest="dev_verb", metavar="COMMAND")
    dev_pack = dev_verbs.add_parser(
        "pack", help="pack a design directory and print its manifest"
    )
    dev_pack.add_argument("directory", type=Path, help="the design pack root")
    dev_pack.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="PATH",
        help="pack-relative path to leave out (repeatable)",
    )
    dev_pack.set_defaults(handler=_dev_pack)
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


def _dev_pack(args: argparse.Namespace) -> int:
    try:
        manifest = pack(args.directory, exclude=args.exclude)
    except (PackError, PipelineError) as e:
        for issue in e.issues:
            print(issue, file=sys.stderr)
        return 1
    if args.json:
        print(manifest.canonical_json())
    else:
        print(json.dumps(manifest.to_dict(), indent=2, sort_keys=True))
    return 0
