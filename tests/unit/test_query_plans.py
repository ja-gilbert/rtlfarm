"""The ready-queue SELECT: its query plan and its ordering.

The claim's inner SELECT must be served by ``tasks_ready_queue`` as a covering
index with no temporary sort, on an empty database, on a populated one, and
after ANALYZE has written statistics. The expected plan is committed as a
fixture so a schema or query change that loses the plan fails by name.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from rtlfarm.clock import DrivenClock
from rtlfarm.db import queries
from rtlfarm.db.connection import Database

FIXTURE = Path(__file__).parent.parent / "fixtures" / "sqlite" / "ready_queue_plan.txt"
DIGEST = "sha256:x"

################################################################################
# Fixtures and Helpers
################################################################################


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = Database(tmp_path / "rtlfarm.db")
    database.migrate(DrivenClock())
    yield database
    database.close()


def _seed(db: Database, rows: list[tuple[str, int, int, int]]) -> None:
    """Insert READY tasks as (task_id, priority, ready_at_ms, not_before_ms)."""

    async def go() -> None:
        async with db.write() as conn:
            conn.execute(
                "INSERT INTO jobs (job_id, design_name, submission_hash, "
                "manifest_json, pipeline_json, selection_json, toolchain_digest, "
                "priority, state, n_tasks, created_at_ms) VALUES ('job-1', 'c', 'h', "
                "'{}', '{}', '{}', ?, 5, 'RUNNING', ?, 1)",
                (DIGEST, len(rows)),
            )
            conn.executemany(
                "INSERT INTO tasks (task_id, job_id, stage_kind, target, seed, tool, "
                "params_json, timeout_s, cacheable, toolchain_digest, state, priority, "
                "ready_at_ms, not_before_ms, max_infra, max_timeout, max_tool_error) "
                "VALUES (?, 'job-1', 'simulate', 't', 1, 'fake', '{}', 60, 1, ?, "
                "'READY', ?, ?, ?, 3, 1, 2)",
                [(t, DIGEST, p, r, n) for t, p, r, n in rows],
            )

    asyncio.run(go())


def _plan(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "EXPLAIN QUERY PLAN " + queries.READY_QUEUE_SELECT,
        {"now": 1_000, "digest": DIGEST},
    ).fetchall()
    return [str(row[3]) for row in rows]


def _head(conn: sqlite3.Connection, now: int) -> str | None:
    row = conn.execute(queries.READY_QUEUE_SELECT, {"now": now, "digest": DIGEST})
    found = row.fetchone()
    return None if found is None else str(found[0])


def _assert_covering(plan: list[str]) -> None:
    assert plan == FIXTURE.read_text(encoding="utf-8").splitlines()
    assert len(plan) == 1
    assert "COVERING INDEX tasks_ready_queue" in plan[0]
    assert all("TEMP B-TREE" not in line for line in plan)


################################################################################
# The Plan
################################################################################


def test_plan_on_an_empty_database(db: Database) -> None:
    reader = db.read()
    _assert_covering(_plan(reader))
    reader.close()


def test_plan_with_rows_and_no_statistics(db: Database) -> None:
    _seed(db, [(f"t{i:03}", i % 10, i, 0) for i in range(200)])
    reader = db.read()
    _assert_covering(_plan(reader))
    reader.close()


def test_plan_after_analyze(db: Database) -> None:
    _seed(db, [(f"t{i:03}", i % 10, i, 0) for i in range(200)])

    async def analyze() -> None:
        async with db.write() as conn:
            conn.execute("ANALYZE")

    asyncio.run(analyze())
    reader = db.read()
    stats = reader.execute(
        "SELECT COUNT(*) FROM sqlite_stat1 WHERE idx = 'tasks_ready_queue'"
    ).fetchone()
    assert stats == (1,)
    _assert_covering(_plan(reader))
    reader.close()


################################################################################
# The Order
################################################################################


def test_highest_priority_wins(db: Database) -> None:
    _seed(db, [("low", 1, 10, 0), ("high", 9, 20, 0), ("mid", 5, 5, 0)])
    reader = db.read()
    assert _head(reader, now=1_000) == "high"
    reader.close()


def test_first_ready_wins_within_a_priority(db: Database) -> None:
    _seed(db, [("later", 5, 20, 0), ("earlier", 5, 10, 0)])
    reader = db.read()
    assert _head(reader, now=1_000) == "earlier"
    reader.close()


def test_task_id_breaks_the_tie(db: Database) -> None:
    _seed(db, [("b", 5, 10, 0), ("a", 5, 10, 0)])
    reader = db.read()
    assert _head(reader, now=1_000) == "a"
    reader.close()


def test_backoff_holds_a_task_back_until_not_before(db: Database) -> None:
    _seed(db, [("held", 9, 1, 5_000), ("free", 1, 2, 0)])
    reader = db.read()
    assert _head(reader, now=4_999) == "free"
    assert _head(reader, now=5_000) == "held"
    reader.close()


def test_other_digests_are_invisible(db: Database) -> None:
    _seed(db, [("mine", 5, 10, 0)])

    async def retag() -> None:
        async with db.write() as conn:
            conn.execute("UPDATE tasks SET toolchain_digest = 'sha256:other'")

    asyncio.run(retag())
    reader = db.read()
    assert _head(reader, now=1_000) is None
    reader.close()
