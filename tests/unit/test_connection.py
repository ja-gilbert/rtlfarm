"""The connection layer: the version floor, the durability modes, one writer
behind an asyncio lock with ``BEGIN IMMEDIATE`` per use, and read-only
connections that never see an open write.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from rtlfarm.clock import DrivenClock
from rtlfarm.db import connection
from rtlfarm.db.connection import Database

################################################################################
# Helpers
################################################################################


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = Database(tmp_path / "rtlfarm.db")
    assert database.migrate(DrivenClock()) == [1]
    yield database
    database.close()


def _count_jobs(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()
    assert row is not None
    return int(row[0])


INSERT_JOB = (
    "INSERT INTO jobs (job_id, design_name, submission_hash, manifest_json, "
    "pipeline_json, selection_json, toolchain_digest, priority, state, n_tasks, "
    "created_at_ms) VALUES (?, 'counter', 'h', '{}', '{}', '{}', 'sha256:x', 5, "
    "'SUBMITTED', 1, 1000)"
)

################################################################################
# Version Floor and Durability Modes
################################################################################


def test_this_build_meets_the_version_floor() -> None:
    assert connection.check_sqlite_version() >= connection.MIN_SQLITE_VERSION


def test_an_old_build_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sqlite3, "sqlite_version_info", (3, 34, 1))
    with pytest.raises(connection.SqliteVersionError, match=r"3\.35\.0"):
        connection.open_connection(tmp_path / "rtlfarm.db")


@pytest.mark.parametrize(("mode", "value"), [("OFF", 0), ("NORMAL", 1), ("FULL", 2)])
def test_synchronous_mode_is_applied(tmp_path: Path, mode: str, value: int) -> None:
    conn = connection.open_connection(
        tmp_path / "rtlfarm.db",
        synchronous=mode,  # type: ignore[arg-type]
    )
    assert conn.execute("PRAGMA synchronous").fetchone() == (value,)
    conn.close()


def test_unknown_synchronous_mode_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="synchronous"):
        connection.open_connection(
            tmp_path / "rtlfarm.db",
            synchronous="EXTRA",  # type: ignore[arg-type]
        )


################################################################################
# The Write Context
################################################################################


def test_write_context_owns_the_transaction(db: Database) -> None:
    async def run() -> None:
        assert not db.in_transaction
        async with db.write() as conn:
            assert conn.in_transaction
            conn.execute(INSERT_JOB, ("job-1",))
            assert conn.in_transaction
        assert not db.in_transaction

    asyncio.run(run())
    reader = db.read()
    assert _count_jobs(reader) == 1
    reader.close()


def test_write_context_rolls_back_on_exception(db: Database) -> None:
    async def run() -> None:
        with pytest.raises(RuntimeError, match="boom"):
            async with db.write() as conn:
                conn.execute(INSERT_JOB, ("job-1",))
                raise RuntimeError("boom")
        assert not db.in_transaction

    asyncio.run(run())
    reader = db.read()
    assert _count_jobs(reader) == 0
    reader.close()


def test_uncommitted_writes_are_invisible_to_readers(db: Database) -> None:
    async def run() -> None:
        reader = db.read()
        async with db.write() as conn:
            conn.execute(INSERT_JOB, ("job-1",))
            assert _count_jobs(reader) == 0
        assert _count_jobs(reader) == 1
        reader.close()

    asyncio.run(run())


def test_write_context_takes_the_write_lock_at_begin(db: Database) -> None:
    """BEGIN IMMEDIATE reserves the database before the first statement, so
    another connection cannot start a write transaction meanwhile."""
    other = sqlite3.connect(db.path, autocommit=True)
    other.execute("PRAGMA busy_timeout = 0")

    async def run() -> None:
        async with db.write():
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                other.execute("BEGIN IMMEDIATE")
        other.execute("BEGIN IMMEDIATE")
        other.execute("ROLLBACK")

    asyncio.run(run())
    other.close()


def test_writers_serialize_under_the_lock(db: Database) -> None:
    order: list[str] = []

    async def run() -> None:
        first_inside = asyncio.Event()
        release_first = asyncio.Event()

        async def first() -> None:
            async with db.write() as conn:
                order.append("first in")
                conn.execute(INSERT_JOB, ("job-1",))
                first_inside.set()
                await release_first.wait()
                order.append("first out")

        async def second() -> None:
            await first_inside.wait()
            async with db.write() as conn:
                order.append("second in")
                conn.execute(INSERT_JOB, ("job-2",))

        task_first = asyncio.create_task(first())
        task_second = asyncio.create_task(second())
        await first_inside.wait()
        await asyncio.sleep(0)
        assert order == ["first in"]
        release_first.set()
        await asyncio.gather(task_first, task_second)

    asyncio.run(run())
    assert order == ["first in", "first out", "second in"]
    reader = db.read()
    assert _count_jobs(reader) == 2
    reader.close()


def test_returning_is_drained_before_the_next_statement(db: Database) -> None:
    async def run() -> None:
        async with db.write() as conn:
            conn.execute(INSERT_JOB, ("job-1",))
            rows = connection.execute_returning(
                conn,
                "UPDATE jobs SET state = 'RUNNING' WHERE job_id = ? RETURNING job_id",
                ("job-1",),
            )
            assert rows == [("job-1",)]
            conn.execute(INSERT_JOB, ("job-2",))

    asyncio.run(run())
    reader = db.read()
    assert _count_jobs(reader) == 2
    reader.close()


################################################################################
# Read Connections
################################################################################


def test_read_connection_cannot_write(db: Database) -> None:
    reader = db.read()
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        reader.execute(INSERT_JOB, ("job-1",))
    reader.close()


def test_read_connection_is_autocommit_with_the_busy_timeout(db: Database) -> None:
    reader = db.read()
    assert reader.autocommit is True
    assert reader.execute("PRAGMA busy_timeout").fetchone() == (
        connection.BUSY_TIMEOUT_MS,
    )
    reader.close()


def test_closed_database_rejects_writes(tmp_path: Path) -> None:
    database = Database(tmp_path / "rtlfarm.db")
    database.close()

    async def run() -> None:
        async with database.write():
            pass

    with pytest.raises(sqlite3.ProgrammingError):
        asyncio.run(run())
