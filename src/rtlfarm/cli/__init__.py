"""The ``rtlfarm`` command: a thin API client plus developer commands.

Only the entry point and the global options exist so far; each verb arrives
together with the server-side behaviour it drives.
"""

from __future__ import annotations

import argparse
import sys


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
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns the process exit code (2 is a usage error)."""
    parser = build_parser()
    parser.parse_args(argv)
    parser.print_help(sys.stderr)
    return 2
