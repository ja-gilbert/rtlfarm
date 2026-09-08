"""``rtlfarm admin``: operator commands against the control plane's data."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from rtlfarm.cli.control import add_config_arguments, load_from_args
from rtlfarm.clock import WallClock
from rtlfarm.config import ConfigError
from rtlfarm.control.startup import DB_FILENAME, StartupError, open_database
from rtlfarm.db.migrate import applied_versions

if TYPE_CHECKING:
    # argparse has no public name for what add_subparsers() returns.
    Subparsers = argparse._SubParsersAction[argparse.ArgumentParser]


def register(verbs: Subparsers) -> None:
    admin = verbs.add_parser("admin", help="operator commands")
    commands = admin.add_subparsers(dest="admin_verb", metavar="COMMAND")
    migrate = commands.add_parser(
        "migrate", help="apply pending migrations to the configured database"
    )
    add_config_arguments(migrate)
    migrate.set_defaults(handler=run_migrate)


def run_migrate(args: argparse.Namespace) -> int:
    try:
        config = load_from_args(args, os.environ)
        data_dir = Path(config.data_dir)
        db, applied = open_database(data_dir, WallClock())
    except (ConfigError, StartupError) as e:
        print(f"rtlfarm admin migrate: {e}", file=sys.stderr)
        return 1
    try:
        reader = db.read()
        try:
            current = applied_versions(reader)
        finally:
            reader.close()
    finally:
        db.close()
    database = data_dir / DB_FILENAME
    if args.json:
        report = {"database": str(database), "applied": applied, "at": current}
        print(json.dumps(report))
    else:
        applied_text = _versions(applied) or "nothing"
        print(f"{database}: applied {applied_text}; at {_versions(current)}")
    return 0


def _versions(versions: list[int]) -> str:
    return ", ".join(str(version) for version in versions)
