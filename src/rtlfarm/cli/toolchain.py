"""``rtlfarm toolchain``: the generated toolchain manifest and its digest."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import TYPE_CHECKING

from rtlfarm.tools import manifest as toolchain_manifest

if TYPE_CHECKING:
    # argparse has no public name for what add_subparsers() returns.
    Subparsers = argparse._SubParsersAction[argparse.ArgumentParser]


def register(verbs: Subparsers) -> None:
    toolchain = verbs.add_parser("toolchain", help="toolchain identity")
    commands = toolchain.add_subparsers(dest="toolchain_verb", metavar="COMMAND")
    gen = commands.add_parser(
        "manifest", help="generate the toolchain manifest by executing the tools"
    )
    gen.add_argument("-o", "--output", type=Path, help="write the manifest here")
    gen.set_defaults(handler=run_manifest)


def run_manifest(args: argparse.Namespace) -> int:
    data = toolchain_manifest.generate()
    if args.output is not None:
        toolchain_manifest.write(data, args.output)
    if args.json:
        print(toolchain_manifest.canonical_json(data))
    else:
        print(json.dumps(data, indent=2, sort_keys=True))
        print(f"digest: {toolchain_manifest.digest(data)}")
    return 0
