"""``rtlfarm control run``: the control plane process."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
from collections.abc import Mapping
from pathlib import Path
from types import FrameType
from typing import TYPE_CHECKING

import uvicorn
from fastapi import FastAPI

from rtlfarm import log
from rtlfarm.clock import WallClock
from rtlfarm.config import (
    DEFAULT_DOTENV,
    DEFAULT_TOML,
    Config,
    ConfigError,
    TimingError,
    load_config,
    read_dotenv,
    validate_timing,
)
from rtlfarm.control.app import InsecureBind, assert_bind_allowed
from rtlfarm.control.startup import StartupError, prepare

if TYPE_CHECKING:
    # argparse has no public name for what add_subparsers() returns.
    Subparsers = argparse._SubParsersAction[argparse.ArgumentParser]


def add_config_arguments(parser: argparse.ArgumentParser) -> None:
    """The two options every server-side verb shares."""
    parser.add_argument(
        "--config",
        type=Path,
        help=f"configuration file (default: {DEFAULT_TOML} when it exists)",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        help=f"dotenv file merged under the environment (default: {DEFAULT_DOTENV})",
    )


def load_from_args(args: argparse.Namespace, environ: Mapping[str, str]) -> Config:
    """Configuration from the file, the dotenv file and the process environment.

    The process environment wins over the dotenv file, which wins over the
    file, which wins over the defaults. A default file that is absent is
    normal; a file named on the command line must exist.
    """
    toml_path: Path | None = args.config
    if toml_path is None and DEFAULT_TOML.is_file():
        toml_path = DEFAULT_TOML
    env_file: Path = DEFAULT_DOTENV
    if args.env_file is not None:
        if not args.env_file.is_file():
            raise ConfigError(f"dotenv file {args.env_file} does not exist")
        env_file = args.env_file
    env = {**read_dotenv(env_file), **environ}
    return load_config(toml_path=toml_path, env=env)


def register(verbs: Subparsers) -> None:
    control = verbs.add_parser("control", help="the control plane")
    commands = control.add_subparsers(dest="control_verb", metavar="COMMAND")
    run = commands.add_parser("run", help="migrate, then serve the API")
    add_config_arguments(run)
    run.add_argument("--host", default="127.0.0.1", help="bind address")
    run.add_argument("--port", type=int, default=8080, help="bind port")
    run.set_defaults(handler=run_control)


def run_control(args: argparse.Namespace) -> int:
    """Load and check the configuration, bring the control plane up, serve.

    Anything an operator can get wrong (the configuration, the timing
    orderings, a non-loopback bind without auth, an unusable volume) is one
    line on stderr and exit code 1. The loopback guard runs before anything
    touches the disk.
    """
    log.configure_logging("control", level=logging.INFO)
    try:
        config = load_from_args(args, os.environ)
        validate_timing(config.timing)
        assert_bind_allowed(config, args.host)
        plane = prepare(config, WallClock())
    except (ConfigError, TimingError, InsecureBind, StartupError) as e:
        print(f"rtlfarm control run: {e}", file=sys.stderr)
        return 1
    try:
        serve(plane.app, args.host, args.port)
    finally:
        plane.close()
    return 0


def serve(app: FastAPI, host: str, port: int) -> None:
    """Run the HTTP server until it is stopped. Replaced by tests.

    uvicorn handles SIGINT and SIGTERM itself: it drains the connections,
    restores the previous handlers and re-raises the signal. A re-raised
    SIGINT becomes ``KeyboardInterrupt``, which uvicorn swallows, so ``run``
    returns and the caller's ``finally`` closes the database. A re-raised
    SIGTERM (``docker stop``) would end the process before that ``finally``
    ran and leave the WAL behind, so for the duration SIGTERM raises
    ``SystemExit`` instead, which unwinds through the ``finally`` normally.
    """
    previous = signal.signal(signal.SIGTERM, _exit_cleanly)
    try:
        uvicorn.run(app, host=host, port=port, log_config=None, access_log=False)
    finally:
        signal.signal(signal.SIGTERM, previous)


def _exit_cleanly(signum: int, frame: FrameType | None) -> None:
    raise SystemExit(0)
