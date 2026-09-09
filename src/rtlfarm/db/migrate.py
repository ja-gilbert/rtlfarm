"""The migration runner: numbered SQL files applied in order, one transaction each.

Migrations live in ``migrations/`` as ``0001.sql``, ``0002_short_name.sql``
and so on, contiguous from 1. A file contains plain SQL and no ``BEGIN`` or
``COMMIT``: the runner opens ``BEGIN IMMEDIATE``, runs the file, records the
version in ``schema_migrations`` and commits, so a failure partway leaves
neither the file's schema changes nor its row. There are no down-migrations.
``ANALYZE`` runs once after any migration is applied so the query planner
has statistics for the new tables and indexes.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from importlib.resources import files
from importlib.resources.abc import Traversable

from rtlfarm.clock import Clock

MIGRATIONS_DIR: Traversable = files("rtlfarm.db") / "migrations"

_FILENAME_RE = re.compile(r"(\d{4})(?:_[a-z0-9_]+)?\.sql")


class MigrationError(RuntimeError):
    """A migration file is malformed, out of sequence, or failed to apply."""


@dataclass(frozen=True)
class Migration:
    """One migration file: its version, its file name, and its SQL."""

    version: int
    name: str
    sql: str


def load_migrations(directory: Traversable = MIGRATIONS_DIR) -> list[Migration]:
    """Read every migration file in ``directory``, ordered by version.

    Files that do not match the naming pattern are ignored. Two files with
    the same version, or versions that are not contiguous from 1, are errors.
    """
    found: dict[int, Migration] = {}
    for entry in directory.iterdir():
        match = _FILENAME_RE.fullmatch(entry.name)
        if match is None:
            continue
        version = int(match.group(1))
        if version in found:
            raise MigrationError(
                f"duplicate migration version {version}: "
                f"{found[version].name} and {entry.name}"
            )
        found[version] = Migration(
            version, entry.name, entry.read_text(encoding="utf-8")
        )
    ordered = [found[v] for v in sorted(found)]
    expected = list(range(1, len(ordered) + 1))
    if [m.version for m in ordered] != expected:
        raise MigrationError(
            f"migration versions must be contiguous from 1, got {sorted(found)}"
        )
    return ordered


def applied_versions(conn: sqlite3.Connection) -> list[int]:
    """The versions recorded in ``schema_migrations``; empty on a fresh database."""
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master "
        "WHERE type = 'table' AND name = 'schema_migrations'"
    ).fetchone()
    if exists is None:
        return []
    rows = conn.execute(
        "SELECT version FROM schema_migrations ORDER BY version"
    ).fetchall()
    return [int(row[0]) for row in rows]


def apply_migrations(
    conn: sqlite3.Connection,
    clock: Clock,
    migrations: Iterable[Migration] | None = None,
) -> list[int]:
    """Apply every migration newer than the latest recorded one; return the versions.

    ``migrations`` defaults to the packaged directory; tests pass their own.
    Each migration runs in one ``BEGIN IMMEDIATE`` … ``COMMIT`` owned here;
    a migration that ends that transaction itself is rejected.
    """
    pending = list(migrations) if migrations is not None else load_migrations()
    done = applied_versions(conn)
    latest = done[-1] if done else 0
    applied: list[int] = []
    for migration in pending:
        if migration.version <= latest:
            continue
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.executescript(migration.sql)
            if not conn.in_transaction:
                raise MigrationError(
                    f"migration {migration.name} ended the transaction it runs in"
                )
            conn.execute(
                "INSERT INTO schema_migrations (version, applied_at_ms) VALUES (?, ?)",
                (migration.version, clock.now_ms()),
            )
            conn.execute("COMMIT")
        except Exception as e:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            if isinstance(e, MigrationError):
                raise
            raise MigrationError(f"migration {migration.name} failed: {e}") from e
        applied.append(migration.version)
    if applied:
        conn.execute("ANALYZE")
    return applied
