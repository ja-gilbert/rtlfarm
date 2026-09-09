"""Opening SQLite connections the way the control plane needs them.

Every connection is opened in autocommit mode, so the ``sqlite3`` module never
begins a transaction on its own; ``Connection.commit()`` and ``rollback()``
are no-ops and are never called. Transactions are controlled only by explicit
SQL: a write path issues ``BEGIN IMMEDIATE`` and ends with ``COMMIT`` or
``ROLLBACK``, which is what makes "one transaction per migration" and "one
transaction per scheduler step" true by construction.

The pragmas set here are operating parameters of the database file, not
scheduler timing: WAL so readers never block the writer, foreign keys on, a
busy timeout so a second process waits instead of failing at once, a page
cache, and the caller's durability level.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal, get_args

from rtlfarm.clock import Clock
from rtlfarm.db.migrate import apply_migrations

#: The oldest SQLite the control plane runs on: ``RETURNING`` arrived in 3.35.
MIN_SQLITE_VERSION: tuple[int, int, int] = (3, 35, 0)

#: Page cache per connection, in KiB (SQLite reads a negative value as KiB).
CACHE_SIZE = -16_000

#: How long a connection waits for a lock held by another process before
#: raising ``sqlite3.OperationalError``. A SQLite driver parameter, not a
#: scheduler timing constant.
BUSY_TIMEOUT_MS = 5_000

#: ``NORMAL`` for a real database, ``OFF`` for a throwaway test database,
#: ``FULL`` where the deployment asks SQLite for its strongest fsync contract.
Synchronous = Literal["OFF", "NORMAL", "FULL"]

_SYNCHRONOUS_MODES: frozenset[str] = frozenset(get_args(Synchronous))


class SqliteVersionError(RuntimeError):
    """The linked SQLite library is older than ``MIN_SQLITE_VERSION``."""


def check_sqlite_version() -> tuple[int, int, int]:
    """Return the linked SQLite version, or raise if it is below the floor."""
    version = sqlite3.sqlite_version_info
    if version < MIN_SQLITE_VERSION:
        floor = ".".join(str(part) for part in MIN_SQLITE_VERSION)
        raise SqliteVersionError(
            f"SQLite {sqlite3.sqlite_version} is too old; {floor} or newer is required"
        )
    return version


def open_connection(
    path: Path, *, synchronous: Synchronous = "NORMAL"
) -> sqlite3.Connection:
    """Open ``path`` in autocommit mode with the control plane's pragmas set.

    The caller owns the connection and closes it. Pragmas are per connection,
    so every connection to the same file goes through here.
    """
    if synchronous not in _SYNCHRONOUS_MODES:
        raise ValueError(f"synchronous must be one of {sorted(_SYNCHRONOUS_MODES)}")
    check_sqlite_version()
    conn = sqlite3.connect(path, autocommit=True)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        conn.execute(f"PRAGMA cache_size = {CACHE_SIZE}")
        conn.execute(f"PRAGMA synchronous = {synchronous}")
    except BaseException:
        conn.close()  # a file that is not a database fails at the first pragma
        raise
    return conn


def open_read_connection(path: Path) -> sqlite3.Connection:
    """Open ``path`` read-only, for readers that must never see an open write.

    Reads on the writer connection would observe its uncommitted transaction;
    a separate connection sees only committed state. Opened through a URI with
    ``mode=ro`` so a stray write is a driver error, not a silent one;
    ``as_uri`` percent-encodes the path, so a ``?`` or ``#`` in a directory
    name is a file-name character rather than URI syntax.
    """
    check_sqlite_version()
    uri = f"{path.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, autocommit=True)
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    conn.execute(f"PRAGMA cache_size = {CACHE_SIZE}")
    return conn


def execute_returning(
    conn: sqlite3.Connection, sql: str, params: Sequence[object] = ()
) -> list[tuple[object, ...]]:
    """Run a statement with ``RETURNING`` and drain it before anything else runs.

    SQLite forbids modifying the database while a ``RETURNING`` statement is
    still being stepped, so the rows are fetched to exhaustion here.
    """
    rows: list[tuple[object, ...]] = conn.execute(sql, params).fetchall()
    return rows


class Database:
    """The control plane's handle on one database file.

    One writer connection, used only inside ``write()``, which serializes
    writers with an asyncio lock and wraps each use in ``BEGIN IMMEDIATE`` …
    ``COMMIT`` (``ROLLBACK`` on any exception). Readers open their own
    read-only connections through ``read()``.
    """

    def __init__(self, path: Path, *, synchronous: Synchronous = "NORMAL") -> None:
        self.path = path
        self._writer = open_connection(path, synchronous=synchronous)
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def write(self) -> AsyncIterator[sqlite3.Connection]:
        """One write transaction: the lock, ``BEGIN IMMEDIATE``, commit or roll back."""
        async with self._lock:
            self._writer.execute("BEGIN IMMEDIATE")
            try:
                yield self._writer
            except BaseException:
                if self._writer.in_transaction:
                    self._writer.execute("ROLLBACK")
                raise
            else:
                self._writer.execute("COMMIT")

    @property
    def in_transaction(self) -> bool:
        """Whether the writer connection has an open transaction."""
        return self._writer.in_transaction

    def migrate(self, clock: Clock) -> list[int]:
        """Apply pending migrations on the writer; each runs in its own transaction."""
        return apply_migrations(self._writer, clock)

    def read(self) -> sqlite3.Connection:
        """A new read-only connection; the caller closes it."""
        return open_read_connection(self.path)

    def close(self) -> None:
        self._writer.close()
