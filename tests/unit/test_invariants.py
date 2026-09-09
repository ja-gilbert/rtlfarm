"""The invariant checker on the invariants that need no scheduler: one
COMMITTED attempt per task, leases and ACTIVE attempts agreeing, terminal
tasks holding no lease, contiguous and current events, acyclic dependencies.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from rtlfarm.clock import DrivenClock
from rtlfarm.control import invariants
from rtlfarm.db.connection import Database

################################################################################
# Fixtures and Row Builders
################################################################################


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = Database(tmp_path / "rtlfarm.db")
    database.migrate(DrivenClock())
    yield database
    database.close()


def _run(db: Database, *statements: tuple[str, tuple[object, ...]]) -> None:
    """Apply raw statements in one write transaction."""

    async def go() -> None:
        async with db.write() as conn:
            for sql, params in statements:
                conn.execute(sql, params)

    asyncio.run(go())


def _job(job_id: str = "job-1", n_tasks: int = 1) -> tuple[str, tuple[object, ...]]:
    """A RUNNING job; ``n_tasks`` must equal the tasks a test seeds under it."""
    return (
        "INSERT INTO jobs (job_id, design_name, submission_hash, manifest_json, "
        "pipeline_json, selection_json, toolchain_digest, priority, state, n_tasks, "
        "created_at_ms) VALUES (?, 'counter', 'h', '{}', '{}', '{}', 'sha256:x', 5, "
        "'RUNNING', ?, 1000)",
        (job_id, n_tasks),
    )


def _task(
    task_id: str, state: str, **columns: object
) -> tuple[str, tuple[object, ...]]:
    row: dict[str, object] = {
        "task_id": task_id,
        "job_id": "job-1",
        "stage_kind": "simulate",
        "target": "tb",
        "seed": 1,
        "tool": "fake",
        "params_json": "{}",
        "timeout_s": 60,
        "cacheable": 1,
        "toolchain_digest": "sha256:x",
        "state": state,
        "priority": 5,
        "ready_at_ms": 1000,
        "max_infra": 3,
        "max_timeout": 1,
        "max_tool_error": 2,
    }
    row.update(columns)
    names = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    return (f"INSERT INTO tasks ({names}) VALUES ({marks})", tuple(row.values()))


def _attempt(
    attempt_id: str, task_id: str, state: str, attempt: int = 1
) -> tuple[str, tuple[object, ...]]:
    return (
        "INSERT INTO attempts (attempt_id, task_id, attempt, worker_id, state, "
        "leased_at_ms) VALUES (?, ?, ?, 'worker-1', ?, 2000)",
        (attempt_id, task_id, attempt, state),
    )


def _event(
    task_id: str, seq: int, to_state: str, from_state: str | None = None
) -> tuple[str, tuple[object, ...]]:
    return (
        "INSERT INTO task_events (task_id, seq, at_ms, actor, from_state, to_state) "
        "VALUES (?, ?, 1000, 'scheduler', ?, ?)",
        (task_id, seq, from_state, to_state),
    )


def _dep(task_id: str, upstream: str) -> tuple[str, tuple[object, ...]]:
    return (
        "INSERT INTO task_deps (task_id, depends_on_task_id, kind) "
        "VALUES (?, ?, 'success')",
        (task_id, upstream),
    )


def _check(db: Database) -> list[invariants.Violation]:
    reader = db.read()
    try:
        return invariants.check_invariants(reader)
    finally:
        reader.close()


def _numbers(db: Database) -> list[int]:
    return sorted(v.invariant for v in _check(db))


def _seed_consistent(db: Database) -> None:
    """A job with a finished task, a leased task and a pending dependent."""
    _run(
        db,
        _job(n_tasks=3),
        _task("t-done", "SUCCEEDED", committed_attempt_id="a-1", finished_at_ms=3000),
        _attempt("a-1", "t-done", "COMMITTED"),
        _event("t-done", 1, "READY", None),
        _event("t-done", 2, "LEASED", "READY"),
        _event("t-done", 3, "SUCCEEDED", "LEASED"),
        _task("t-leased", "LEASED", lease_attempt_id="a-2", leased_by="worker-1"),
        _attempt("a-2", "t-leased", "ACTIVE"),
        _event("t-leased", 1, "READY", None),
        _event("t-leased", 2, "LEASED", "READY"),
        _task("t-pending", "PENDING", ready_at_ms=None),
        _event("t-pending", 1, "PENDING", None),
        _dep("t-pending", "t-done"),
        _dep("t-pending", "t-leased"),
    )


################################################################################
# Passing Databases
################################################################################


def test_empty_database_has_no_violations(db: Database) -> None:
    assert _check(db) == []


def test_consistent_database_has_no_violations(db: Database) -> None:
    _seed_consistent(db)
    assert _check(db) == []


def test_assert_invariants_is_quiet_when_consistent(db: Database) -> None:
    _seed_consistent(db)
    reader = db.read()
    invariants.assert_invariants(reader, DrivenClock())
    assert not reader.in_transaction
    reader.close()


def test_checker_works_on_a_read_only_connection(db: Database) -> None:
    reader = db.read()
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        reader.execute("DELETE FROM jobs")
    assert invariants.check_invariants(reader) == []
    reader.close()


################################################################################
# Invariant 1: One COMMITTED Attempt per Task
################################################################################


def test_two_committed_attempts_are_reported(db: Database) -> None:
    """The index normally forbids this; the checker must catch it without it."""
    _run(
        db,
        ("DROP INDEX attempts_one_committed", ()),
        _job(),
        _task("t", "SUCCEEDED", committed_attempt_id="a-1"),
        _attempt("a-1", "t", "COMMITTED", 1),
        _attempt("a-2", "t", "COMMITTED", 2),
        _event("t", 1, "SUCCEEDED"),
    )
    assert _numbers(db) == [1]
    assert "2 COMMITTED" in _check(db)[0].message


################################################################################
# Invariant 2: Leases and ACTIVE Attempts Agree
################################################################################


@pytest.mark.parametrize(
    "attempt_rows",
    [[_attempt("a-1", "t", "EXPIRED")], []],
    ids=["names-an-expired-attempt", "names-no-row-at-all"],
)
def test_leased_task_without_a_matching_active_attempt(
    db: Database, attempt_rows: list[tuple[str, tuple[object, ...]]]
) -> None:
    _run(
        db,
        _job(),
        _task("t", "LEASED", lease_attempt_id="a-1"),
        *attempt_rows,
        _event("t", 1, "LEASED"),
    )
    assert _numbers(db) == [2]


def test_leased_task_with_two_active_attempts(db: Database) -> None:
    """Exactly one ACTIVE attempt: both halves of the rule fire, and the extra
    attempt is reported by name."""
    _run(
        db,
        _job(),
        _task("t", "RUNNING", lease_attempt_id="a-1"),
        _attempt("a-1", "t", "ACTIVE", 1),
        _attempt("a-2", "t", "ACTIVE", 2),
        _event("t", 1, "RUNNING"),
    )
    violations = _check(db)
    assert [v.invariant for v in violations] == [2, 2]
    assert any("a-2" in v.message for v in violations)


def test_active_attempt_on_a_task_that_is_not_leased(db: Database) -> None:
    _run(
        db,
        _job(),
        _task("t", "READY"),
        _attempt("a-1", "t", "ACTIVE"),
        _event("t", 1, "READY"),
    )
    assert _numbers(db) == [2]


################################################################################
# Invariant 3: Terminal Tasks Hold No Lease
################################################################################


@pytest.mark.parametrize(
    "column", ["leased_by", "lease_expires_at_ms", "leased_at_ms", "started_at_ms"]
)
def test_terminal_task_with_a_leftover_column(db: Database, column: str) -> None:
    value: object = "worker-1" if column == "leased_by" else 5000
    _run(
        db,
        _job(),
        _task("t", "FAILED", **{column: value}),
        _event("t", 1, "FAILED"),
    )
    assert _numbers(db) == [3]


################################################################################
# Invariant 8: Events Are Contiguous and Current
################################################################################


@pytest.mark.parametrize("seqs", [[1, 3], [2]], ids=["gap", "starts-at-two"])
def test_a_non_contiguous_event_sequence_is_reported(
    db: Database, seqs: list[int]
) -> None:
    _run(db, _job(), _task("t", "READY"), *[_event("t", s, "READY") for s in seqs])
    assert _numbers(db) == [8]


def test_last_event_disagreeing_with_task_state_is_reported(db: Database) -> None:
    _run(
        db,
        _job(),
        _task("t", "READY"),
        _event("t", 1, "PENDING"),
    )
    violations = _check(db)
    assert [v.invariant for v in violations] == [8]
    assert "READY" in violations[0].message and "PENDING" in violations[0].message


################################################################################
# Invariant 9: Dependencies Are Acyclic
################################################################################


def test_dependency_cycle_is_reported(db: Database) -> None:
    _run(
        db,
        _job(n_tasks=3),
        _task("a", "PENDING", ready_at_ms=None),
        _task("b", "PENDING", ready_at_ms=None),
        _task("c", "PENDING", ready_at_ms=None),
        _event("a", 1, "PENDING"),
        _event("b", 1, "PENDING"),
        _event("c", 1, "PENDING"),
        _dep("b", "a"),
        _dep("c", "b"),
        _dep("a", "c"),
    )
    assert _numbers(db) == [9]


def test_diamond_dependencies_are_not_a_cycle(db: Database) -> None:
    _run(
        db,
        _job(n_tasks=4),
        _task("a", "PENDING", ready_at_ms=None),
        _task("b", "PENDING", ready_at_ms=None),
        _task("c", "PENDING", ready_at_ms=None),
        _task("d", "PENDING", ready_at_ms=None),
        _event("a", 1, "PENDING"),
        _event("b", 1, "PENDING"),
        _event("c", 1, "PENDING"),
        _event("d", 1, "PENDING"),
        _dep("b", "a"),
        _dep("c", "a"),
        _dep("d", "b"),
        _dep("d", "c"),
    )
    assert _check(db) == []


################################################################################
# Reporting
################################################################################


def test_every_violation_is_reported_together(db: Database) -> None:
    _run(
        db,
        _job(),
        _task("t", "SUCCEEDED", started_at_ms=5000),
        _event("t", 2, "READY"),
    )
    reader = db.read()
    with pytest.raises(invariants.InvariantViolation) as info:
        invariants.assert_invariants(reader, DrivenClock())
    reader.close()
    assert sorted(v.invariant for v in info.value.violations) == [3, 8, 8]
    text = str(info.value)
    assert "invariant 3" in text and "invariant 8" in text
