"""The migration runner: numbered SQL files, applied in order, each in one
transaction owned by the runner.

A migration file contains no BEGIN or COMMIT of its own: the runner opens
BEGIN IMMEDIATE, runs the file, records the version in schema_migrations and
commits, so a failure partway leaves neither the schema changes nor the row.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from rtlfarm.clock import DrivenClock
from rtlfarm.db import connection, migrate

################################################################################
# Helpers
################################################################################


def _open(tmp_path: Path) -> sqlite3.Connection:
    return connection.open_connection(tmp_path / "rtlfarm.db")


def _write(directory: Path, name: str, sql: str) -> None:
    directory.mkdir(exist_ok=True)
    (directory / name).write_text(sql, encoding="utf-8")


def _tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {str(row[0]) for row in rows}


def _recorded(conn: sqlite3.Connection) -> list[tuple[int, int]]:
    rows = conn.execute(
        "SELECT version, applied_at_ms FROM schema_migrations ORDER BY version"
    ).fetchall()
    return [(int(row[0]), int(row[1])) for row in rows]


SCHEMA_MIGRATIONS = (
    "CREATE TABLE schema_migrations "
    "(version INTEGER PRIMARY KEY, applied_at_ms INTEGER NOT NULL);\n"
)

################################################################################
# Ordering and the Transaction Boundary
################################################################################


def test_migrations_apply_in_version_order(tmp_path: Path) -> None:
    src = tmp_path / "migrations"
    _write(src, "0001.sql", SCHEMA_MIGRATIONS + "CREATE TABLE a (x INTEGER);")
    _write(src, "0002_add_b.sql", "CREATE TABLE b (x INTEGER);")
    _write(src, "0003.sql", "CREATE TABLE c (x INTEGER);")
    conn = _open(tmp_path)
    clock = DrivenClock(start_ms=100)
    applied = migrate.apply_migrations(conn, clock, migrate.load_migrations(src))
    assert applied == [1, 2, 3]
    assert _tables(conn) == {"schema_migrations", "a", "b", "c"}
    assert _recorded(conn) == [(1, 100), (2, 100), (3, 100)]  # the injected clock
    conn.close()


def test_only_pending_versions_are_applied(tmp_path: Path) -> None:
    src = tmp_path / "migrations"
    _write(src, "0001.sql", SCHEMA_MIGRATIONS + "CREATE TABLE a (x INTEGER);")
    conn = _open(tmp_path)
    migrate.apply_migrations(conn, DrivenClock(), migrate.load_migrations(src))
    _write(src, "0002.sql", "CREATE TABLE b (x INTEGER);")
    applied = migrate.apply_migrations(
        conn, DrivenClock(start_ms=7), migrate.load_migrations(src)
    )
    assert applied == [2]
    assert _recorded(conn)[-1] == (2, 7)
    conn.close()


def test_failed_migration_leaves_neither_schema_nor_row(tmp_path: Path) -> None:
    """The runner owns the transaction: a failure partway is rolled back whole."""
    src = tmp_path / "migrations"
    _write(src, "0001.sql", SCHEMA_MIGRATIONS + "CREATE TABLE a (x INTEGER);")
    _write(src, "0002.sql", "CREATE TABLE b (x INTEGER);\nCREATE TABLE b (y INTEGER);")
    conn = _open(tmp_path)
    with pytest.raises(migrate.MigrationError, match="0002"):
        migrate.apply_migrations(conn, DrivenClock(), migrate.load_migrations(src))
    assert not conn.in_transaction
    assert _tables(conn) == {"schema_migrations", "a"}
    assert [v for v, _ in _recorded(conn)] == [1]
    conn.close()


def test_migration_that_commits_on_its_own_is_rejected(tmp_path: Path) -> None:
    src = tmp_path / "migrations"
    _write(src, "0001.sql", SCHEMA_MIGRATIONS + "COMMIT;\nCREATE TABLE a (x INTEGER);")
    conn = _open(tmp_path)
    with pytest.raises(migrate.MigrationError, match="transaction"):
        migrate.apply_migrations(conn, DrivenClock(), migrate.load_migrations(src))
    assert not conn.in_transaction
    assert _recorded(conn) == []
    conn.close()


################################################################################
# The Loader
################################################################################


def test_loader_rejects_a_gap_in_the_versions(tmp_path: Path) -> None:
    """A gap would silently skip a migration and leave a table missing."""
    src = tmp_path / "migrations"
    _write(src, "0001.sql", "-- one")
    _write(src, "0003.sql", "-- three")
    with pytest.raises(migrate.MigrationError, match="contiguous"):
        migrate.load_migrations(src)


def test_loader_rejects_a_duplicate_version(tmp_path: Path) -> None:
    src = tmp_path / "migrations"
    _write(src, "0001.sql", "-- one")
    _write(src, "0001_again.sql", "-- one again")
    with pytest.raises(migrate.MigrationError, match="duplicate"):
        migrate.load_migrations(src)
