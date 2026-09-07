"""Migration 0001: the constraints that make the state machines fail loudly.

The schema is the state machine. These tests drive the raw DDL with plain
INSERT and UPDATE statements and assert that each CHECK constraint and
partial unique index rejects exactly the rows it exists to reject, and
accepts the legal neighbors, so a forgotten SET list or a widened state
set fails at the first test rather than silently in production.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from importlib.resources import files
from pathlib import Path

import pytest

from rtlfarm.db import connection

SCHEMA_SQL = files("rtlfarm.db") / "migrations" / "0001.sql"


@pytest.fixture
def db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    conn = connection.open_connection(tmp_path / "rtlfarm.db")
    conn.executescript(SCHEMA_SQL.read_text(encoding="utf-8"))
    yield conn
    conn.close()


################################################################################
# Row Builders
################################################################################


def _insert(conn: sqlite3.Connection, table: str, row: dict[str, object]) -> None:
    columns = ", ".join(row)
    placeholders = ", ".join(f":{name}" for name in row)
    conn.execute(f"INSERT INTO {table} ({columns}) VALUES ({placeholders})", row)


def _insert_job(conn: sqlite3.Connection, job_id: str = "job-1") -> None:
    _insert(
        conn,
        "jobs",
        {
            "job_id": job_id,
            "design_name": "counter",
            "submission_hash": "a" * 64,
            "manifest_json": "{}",
            "pipeline_json": "{}",
            "selection_json": "{}",
            "toolchain_digest": "sha256:" + "b" * 64,
            "priority": 5,
            "state": "SUBMITTED",
            "n_tasks": 1,
            "created_at_ms": 1_000,
        },
    )


def _insert_task(
    conn: sqlite3.Connection, task_id: str = "task-1", **overrides: object
) -> None:
    """Insert a READY task whose every NOT NULL column is set; override to taste."""
    row: dict[str, object] = {
        "task_id": task_id,
        "job_id": "job-1",
        "stage_kind": "simulate",
        "target": "tb_counter",
        "seed": 1,
        "tool": "iverilog",
        "params_json": "{}",
        "timeout_s": 60,
        "cacheable": 1,
        "toolchain_digest": "sha256:" + "b" * 64,
        "state": "READY",
        "priority": 5,
        "ready_at_ms": 1_000,
        "max_infra": 3,
        "max_timeout": 1,
        "max_tool_error": 2,
    }
    row.update(overrides)
    _insert(conn, "tasks", row)


def _insert_attempt(
    conn: sqlite3.Connection, attempt_id: str, state: str, attempt: int = 1
) -> None:
    _insert(
        conn,
        "attempts",
        {
            "attempt_id": attempt_id,
            "task_id": "task-1",
            "attempt": attempt,
            "worker_id": "worker-1",
            "state": state,
            "leased_at_ms": 2_000,
        },
    )


def _lease(conn: sqlite3.Connection, task_id: str = "task-1") -> None:
    """Move a READY task to LEASED the way the claim does."""
    conn.execute(
        """
        UPDATE tasks
        SET state = 'LEASED', leased_by = 'worker-1', lease_attempt_id = 'att-1',
            lease_expires_at_ms = 5_000, leased_at_ms = 2_000, attempt = 1
        WHERE task_id = :task_id
        """,
        {"task_id": task_id},
    )


def _task_state(conn: sqlite3.Connection, task_id: str = "task-1") -> str:
    row = conn.execute(
        "SELECT state FROM tasks WHERE task_id = ?", (task_id,)
    ).fetchone()
    assert row is not None
    state: str = row[0]
    return state


################################################################################
# The Lease CHECK, Both Directions
################################################################################


def test_leased_task_without_lease_attempt_id_is_rejected(
    db: sqlite3.Connection,
) -> None:
    _insert_job(db)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        _insert_task(db, state="LEASED", lease_attempt_id=None)


def test_leased_task_with_lease_attempt_id_is_accepted(
    db: sqlite3.Connection,
) -> None:
    _insert_job(db)
    _insert_task(db, state="LEASED", lease_attempt_id="att-1", leased_by="worker-1")
    assert _task_state(db) == "LEASED"


def test_running_task_without_lease_attempt_id_is_rejected(
    db: sqlite3.Connection,
) -> None:
    _insert_job(db)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        _insert_task(db, state="RUNNING", lease_attempt_id=None)


def test_leaving_leased_without_clearing_the_lease_is_rejected(
    db: sqlite3.Connection,
) -> None:
    """The forgotten SET list: requeue that leaves lease_attempt_id behind."""
    _insert_job(db)
    _insert_task(db)
    _lease(db)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        db.execute(
            "UPDATE tasks SET state = 'READY', requeued_at_ms = 6_000 "
            "WHERE task_id = 'task-1'"
        )
    assert _task_state(db) == "LEASED"


def test_terminal_state_with_leftover_lease_is_rejected(db: sqlite3.Connection) -> None:
    _insert_job(db)
    _insert_task(db)
    _lease(db)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        db.execute(
            "UPDATE tasks SET state = 'SUCCEEDED', finished_at_ms = 6_000 "
            "WHERE task_id = 'task-1'"
        )
    assert _task_state(db) == "LEASED"


def test_leaving_leased_with_the_full_null_list_is_accepted(
    db: sqlite3.Connection,
) -> None:
    _insert_job(db)
    _insert_task(db)
    _lease(db)
    db.execute(
        """
        UPDATE tasks
        SET state = 'READY', requeued_at_ms = 6_000, leased_by = NULL,
            lease_attempt_id = NULL, lease_expires_at_ms = NULL,
            leased_at_ms = NULL, started_at_ms = NULL
        WHERE task_id = 'task-1'
        """
    )
    assert _task_state(db) == "READY"


def test_inserting_ready_with_a_lease_attempt_id_is_rejected(
    db: sqlite3.Connection,
) -> None:
    _insert_job(db)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        _insert_task(db, state="READY", lease_attempt_id="att-1")


################################################################################
# READY Needs ready_at_ms
################################################################################


def test_ready_task_without_ready_at_ms_is_rejected(db: sqlite3.Connection) -> None:
    _insert_job(db)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        _insert_task(db, state="READY", ready_at_ms=None)


def test_pending_task_without_ready_at_ms_is_accepted(db: sqlite3.Connection) -> None:
    _insert_job(db)
    _insert_task(db, state="PENDING", ready_at_ms=None)
    assert _task_state(db) == "PENDING"


################################################################################
# No 'any' Digest
################################################################################


def test_task_toolchain_digest_any_is_rejected(db: sqlite3.Connection) -> None:
    _insert_job(db)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        _insert_task(db, toolchain_digest="any")


################################################################################
# Closed State Sets
################################################################################


def test_unknown_task_state_is_rejected(db: sqlite3.Connection) -> None:
    _insert_job(db)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        _insert_task(db, state="DONE")


def test_aborted_is_an_accepted_attempt_state(db: sqlite3.Connection) -> None:
    """Reserved for a worker-acknowledged cancel; in the CHECK from the start
    because SQLite cannot widen a CHECK later without rebuilding the table."""
    _insert_job(db)
    _insert_task(db)
    _insert_attempt(db, "att-1", "ABORTED")
    row = db.execute("SELECT state FROM attempts WHERE attempt_id = 'att-1'").fetchone()
    assert row == ("ABORTED",)


def test_unknown_attempt_state_is_rejected(db: sqlite3.Connection) -> None:
    _insert_job(db)
    _insert_task(db)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        _insert_attempt(db, "att-1", "CANCELLED")


################################################################################
# One COMMITTED Attempt per Task
################################################################################


def test_second_committed_attempt_for_a_task_is_rejected(
    db: sqlite3.Connection,
) -> None:
    _insert_job(db)
    _insert_task(db)
    _insert_attempt(db, "att-1", "COMMITTED", attempt=1)
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
        _insert_attempt(db, "att-2", "COMMITTED", attempt=2)


def test_promoting_a_second_attempt_to_committed_is_rejected(
    db: sqlite3.Connection,
) -> None:
    _insert_job(db)
    _insert_task(db)
    _insert_attempt(db, "att-1", "COMMITTED", attempt=1)
    _insert_attempt(db, "att-2", "ACTIVE", attempt=2)
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
        db.execute("UPDATE attempts SET state = 'COMMITTED' WHERE attempt_id = 'att-2'")


def test_several_non_committed_attempts_per_task_are_accepted(
    db: sqlite3.Connection,
) -> None:
    """The index is partial: only COMMITTED rows count."""
    _insert_job(db)
    _insert_task(db)
    _insert_attempt(db, "att-1", "EXPIRED", attempt=1)
    _insert_attempt(db, "att-2", "REPORTED", attempt=2)
    _insert_attempt(db, "att-3", "COMMITTED", attempt=3)
    count = db.execute("SELECT COUNT(*) FROM attempts").fetchone()
    assert count == (3,)


def test_committed_attempts_of_different_tasks_are_accepted(
    db: sqlite3.Connection,
) -> None:
    _insert_job(db)
    _insert_task(db, "task-1")
    _insert_task(db, "task-2")
    _insert_attempt(db, "att-1", "COMMITTED")
    db.execute(
        "INSERT INTO attempts (attempt_id, task_id, attempt, worker_id, state, "
        "leased_at_ms) VALUES ('att-2', 'task-2', 1, 'worker-1', 'COMMITTED', 2000)"
    )
    count = db.execute("SELECT COUNT(*) FROM attempts").fetchone()
    assert count == (2,)


################################################################################
# The Connection
################################################################################


def test_connection_is_autocommit_with_no_implicit_begin(
    db: sqlite3.Connection,
) -> None:
    """sqlite3 opens no transaction of its own: a bare INSERT is committed at
    once and visible from a second connection."""
    assert db.autocommit is True
    assert not db.in_transaction
    _insert_job(db)
    assert not db.in_transaction
    other = sqlite3.connect(db.execute("PRAGMA database_list").fetchone()[2])
    try:
        assert other.execute("SELECT COUNT(*) FROM jobs").fetchone() == (1,)
    finally:
        other.close()


def test_pragmas_and_wal(db: sqlite3.Connection) -> None:
    def pragma(name: str) -> object:
        return db.execute(f"PRAGMA {name}").fetchone()[0]

    assert pragma("journal_mode") == "wal"
    assert pragma("foreign_keys") == 1
    assert pragma("busy_timeout") == 5000
    assert pragma("synchronous") == 1  # NORMAL, the default for a real database
    assert pragma("cache_size") == connection.CACHE_SIZE


def test_foreign_keys_are_enforced(db: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
        _insert_task(db, job_id="no-such-job")
