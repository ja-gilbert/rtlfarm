"""Migration 0001: the constraints that make the state machines fail loudly.

These tests drive the raw DDL with plain INSERT and UPDATE, asserting that
each CHECK constraint and partial unique index rejects exactly the rows it
exists to reject and accepts the legal neighbors.
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


def _insert_job(
    conn: sqlite3.Connection, job_id: str = "job-1", **overrides: object
) -> None:
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
            **overrides,
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
            lease_expires_at_ms = 5000, leased_at_ms = 2000, attempt = 1
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


def test_a_leased_state_without_a_lease_attempt_id_is_rejected(
    db: sqlite3.Connection,
) -> None:
    """A lease without its fencing token could never be fenced."""
    _insert_job(db)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        _insert_task(db, state="LEASED", lease_attempt_id=None)


def test_leaving_leased_without_clearing_the_lease_is_rejected(
    db: sqlite3.Connection,
) -> None:
    """The forgotten SET list: a transition out of LEASED that leaves
    lease_attempt_id behind fails the CHECK."""
    _insert_job(db)
    _insert_task(db)
    _lease(db)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        db.execute(
            "UPDATE tasks SET state = 'READY', requeued_at_ms = 6000 "
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
        SET state = 'READY', requeued_at_ms = 6000, leased_by = NULL,
            lease_attempt_id = NULL, lease_expires_at_ms = NULL,
            leased_at_ms = NULL, started_at_ms = NULL
        WHERE task_id = 'task-1'
        """
    )
    assert _task_state(db) == "READY"


################################################################################
# READY Needs ready_at_ms
################################################################################


def test_ready_task_without_ready_at_ms_is_rejected(db: sqlite3.Connection) -> None:
    _insert_job(db)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        _insert_task(db, state="READY", ready_at_ms=None)


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


################################################################################
# One Job per Idempotency Key
################################################################################


def test_two_jobs_with_the_same_idempotency_key_are_rejected(
    db: sqlite3.Connection,
) -> None:
    """The last resort behind replay: a race that slipped past the read in
    submit could otherwise file two jobs for one key."""
    _insert_job(db, "job-1", idempotency_key="k")
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
        _insert_job(db, "job-2", idempotency_key="k")


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


################################################################################
# WAL and Foreign Keys
################################################################################


def test_pragmas_and_wal(db: sqlite3.Connection) -> None:
    """Without WAL, readers block the writer and the control plane stalls;
    a real database that silently opened with synchronous OFF would lose
    committed jobs on power loss."""

    def pragma(name: str) -> object:
        return db.execute(f"PRAGMA {name}").fetchone()[0]

    assert pragma("journal_mode") == "wal"
    assert pragma("synchronous") == 1  # NORMAL, the default for a real database
    assert pragma("busy_timeout") == connection.BUSY_TIMEOUT_MS
    assert pragma("cache_size") == connection.CACHE_SIZE


def test_foreign_keys_are_enforced(db: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
        _insert_task(db, job_id="no-such-job")
