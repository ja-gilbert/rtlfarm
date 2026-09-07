"""The migration runner: numbered SQL files, applied in order, each in one
transaction owned by the runner.

A migration file contains no BEGIN or COMMIT. The runner opens BEGIN IMMEDIATE,
runs the file, records the version in schema_migrations and commits, so a
failure partway leaves neither the file's schema changes nor its row. Versions
are contiguous from 1, a second run is a no-op, and ANALYZE runs after any
migration is applied.
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
# The Shipped Migrations
################################################################################


def test_fresh_database_gets_every_shipped_migration(tmp_path: Path) -> None:
    conn = _open(tmp_path)
    clock = DrivenClock(start_ms=12_345)
    applied = migrate.apply_migrations(conn, clock)
    assert applied == [1]
    assert _recorded(conn) == [(1, 12_345)]
    assert {"jobs", "tasks", "attempts", "task_events", "cache_entries"} <= _tables(
        conn
    )
    conn.close()


def test_second_run_is_a_no_op(tmp_path: Path) -> None:
    conn = _open(tmp_path)
    migrate.apply_migrations(conn, DrivenClock(start_ms=1))
    before = _recorded(conn)
    assert migrate.apply_migrations(conn, DrivenClock(start_ms=2)) == []
    assert _recorded(conn) == before
    conn.close()


def test_shipped_migrations_are_contiguous_from_one() -> None:
    versions = [m.version for m in migrate.load_migrations()]
    assert versions == list(range(1, len(versions) + 1))
    assert versions[0] == 1


def test_analyze_runs_after_a_migration(tmp_path: Path) -> None:
    conn = _open(tmp_path)
    migrate.apply_migrations(conn, DrivenClock())
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE name = 'sqlite_stat1'"
    ).fetchone()
    assert row == ("sqlite_stat1",)
    conn.close()


def test_runner_leaves_no_open_transaction(tmp_path: Path) -> None:
    conn = _open(tmp_path)
    migrate.apply_migrations(conn, DrivenClock())
    assert not conn.in_transaction
    conn.close()


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
    assert [v for v, _ in _recorded(conn)] == [1, 2, 3]
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


def test_applied_at_ms_comes_from_the_injected_clock(tmp_path: Path) -> None:
    src = tmp_path / "migrations"
    _write(src, "0001.sql", SCHEMA_MIGRATIONS)
    _write(src, "0002.sql", "CREATE TABLE a (x INTEGER);")
    conn = _open(tmp_path)
    clock = DrivenClock(start_ms=1_000)
    migrate.apply_migrations(conn, clock, migrate.load_migrations(src))
    assert _recorded(conn) == [(1, 1_000), (2, 1_000)]
    conn.close()


################################################################################
# The Loader
################################################################################


def test_loader_orders_by_version_and_keeps_names(tmp_path: Path) -> None:
    src = tmp_path / "migrations"
    _write(src, "0002_second.sql", "-- two")
    _write(src, "0001.sql", "-- one")
    loaded = migrate.load_migrations(src)
    assert [(m.version, m.name) for m in loaded] == [
        (1, "0001.sql"),
        (2, "0002_second.sql"),
    ]
    assert loaded[0].sql == "-- one"


def test_loader_ignores_files_that_are_not_migrations(tmp_path: Path) -> None:
    src = tmp_path / "migrations"
    _write(src, "0001.sql", "-- one")
    _write(src, "README.md", "not sql")
    _write(src, "notes.sql", "no version")
    assert [m.version for m in migrate.load_migrations(src)] == [1]


def test_loader_rejects_a_gap(tmp_path: Path) -> None:
    src = tmp_path / "migrations"
    _write(src, "0001.sql", "-- one")
    _write(src, "0003.sql", "-- three")
    with pytest.raises(migrate.MigrationError, match="contiguous"):
        migrate.load_migrations(src)


def test_loader_rejects_a_directory_that_does_not_start_at_one(tmp_path: Path) -> None:
    src = tmp_path / "migrations"
    _write(src, "0002.sql", "-- two")
    with pytest.raises(migrate.MigrationError, match="contiguous"):
        migrate.load_migrations(src)


def test_loader_rejects_a_duplicate_version(tmp_path: Path) -> None:
    src = tmp_path / "migrations"
    _write(src, "0001.sql", "-- one")
    _write(src, "0001_again.sql", "-- one again")
    with pytest.raises(migrate.MigrationError, match="duplicate"):
        migrate.load_migrations(src)
