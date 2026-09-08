"""``rtlfarm dev``: developer commands that need no control plane."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from rtlfarm.config import DEFAULT_DOTENV
from rtlfarm.expand.pack import PackError, pack
from rtlfarm.expand.pipeline import PipelineError
from rtlfarm.tools import manifest as toolchain_manifest

if TYPE_CHECKING:
    # argparse has no public name for what add_subparsers() returns.
    Subparsers = argparse._SubParsersAction[argparse.ArgumentParser]


def register(verbs: Subparsers) -> None:
    dev = verbs.add_parser("dev", help="developer commands; no control plane needed")
    commands = dev.add_subparsers(dest="dev_verb", metavar="COMMAND")

    pack_cmd = commands.add_parser(
        "pack", help="pack a design directory and print its manifest"
    )
    pack_cmd.add_argument("directory", type=Path, help="the design pack root")
    pack_cmd.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="PATH",
        help="pack-relative path to leave out (repeatable)",
    )
    pack_cmd.set_defaults(handler=run_pack)

    pin = commands.add_parser(
        "pin-toolchain",
        help="write the toolchain digest into .env so jobs are pinned to it",
    )
    pin.add_argument(
        "--manifest",
        type=Path,
        help="a generated toolchain manifest; default: generate one now",
    )
    pin.add_argument(
        "--env", type=Path, default=DEFAULT_DOTENV, help="the dotenv file to write"
    )
    pin.set_defaults(handler=run_pin_toolchain)


def run_pack(args: argparse.Namespace) -> int:
    try:
        packed = pack(args.directory, exclude=args.exclude)
    except (PackError, PipelineError) as e:
        for issue in e.issues:
            print(issue, file=sys.stderr)
        return 1
    if args.json:
        print(packed.canonical_json())
    else:
        print(json.dumps(packed.to_dict(), indent=2, sort_keys=True))
    return 0


def run_pin_toolchain(args: argparse.Namespace) -> int:
    if args.manifest is not None:
        data = toolchain_manifest.read(args.manifest)
    else:
        data = toolchain_manifest.generate()
    value = toolchain_manifest.digest(data)
    toolchain_manifest.pin_env(args.env, value)
    print(json.dumps({"digest": value, "env": str(args.env)}) if args.json else value)
    return 0
