"""Bringing a control plane up: the steps between configuration and serving.

``open_database`` creates the data directory, opens the database in it and
applies pending migrations; ``rtlfarm admin migrate`` stops there.
``prepare`` goes on to everything else that must be true before the first
request and that a test can check without a socket: stale uploads are swept,
the application is built, readiness is set, and the startup line and the
auth banner are logged. ``rtlfarm control run`` calls it and then hands the
application to the HTTP server.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI

from rtlfarm import log
from rtlfarm.clock import Clock
from rtlfarm.config import SECRET_FIELDS, Config
from rtlfarm.control.app import Services, auth_enabled, create_app
from rtlfarm.control.blobstore import BlobStore
from rtlfarm.db.connection import Database, SqliteVersionError, Synchronous
from rtlfarm.db.migrate import MigrationError

logger = log.get_logger("rtlfarm.control")

DB_FILENAME = "rtlfarm.db"
BLOBS_DIRNAME = "blobs"


class StartupError(RuntimeError):
    """The data directory or the database cannot be brought up.

    The message names the path and the cause, for the operator; the original
    exception is chained for anyone who needs the traceback.
    """


@dataclass(frozen=True)
class ControlPlane:
    config: Config
    app: FastAPI
    db: Database
    blobs: BlobStore

    def close(self) -> None:
        self.db.close()


def config_fingerprint(config: Config) -> str:
    """A short digest of the configuration with the secrets blanked, so a
    startup log line says which configuration is in force without leaking it."""
    data = dataclasses.asdict(config)
    for name in SECRET_FIELDS:
        data[name] = None if data[name] is None else "<set>"
    text = json.dumps(data, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def open_database(
    data_dir: Path, clock: Clock, *, synchronous: Synchronous = "NORMAL"
) -> tuple[Database, list[int]]:
    """Create ``data_dir``, open the database in it and apply pending migrations.

    Returns the open database and the versions applied now. Shared by
    ``control run`` and ``admin migrate`` so both agree on where the database
    lives. Every failure an operator can cause (an unwritable volume, a path
    that is a file, a locked or corrupt database, an old SQLite) is a
    ``StartupError``; a writer this function opened is closed before it is
    raised.
    """
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise StartupError(f"{data_dir}: {e}") from e
    database = data_dir / DB_FILENAME
    try:
        db = Database(database, synchronous=synchronous)
    except (sqlite3.Error, SqliteVersionError) as e:
        raise StartupError(f"{database}: {e}") from e
    try:
        applied = db.migrate(clock)
    except (sqlite3.Error, MigrationError) as e:
        db.close()
        raise StartupError(f"{database}: {e}") from e
    return db, applied


def prepare(
    config: Config, clock: Clock, *, synchronous: Synchronous = "NORMAL"
) -> ControlPlane:
    """Migrate, sweep, build the app, log the startup line; nothing listens yet."""
    data_dir = Path(config.data_dir)
    db, applied = open_database(data_dir, clock, synchronous=synchronous)
    try:
        blobs = BlobStore(data_dir / BLOBS_DIRNAME, config.blobs)
        swept = blobs.sweep_tmp(
            older_than_s=config.timing.upload_timeout_s, now_s=clock.now_ms() / 1000
        )
    except OSError as e:
        db.close()
        raise StartupError(f"{data_dir / BLOBS_DIRNAME}: {e}") from e
    app = create_app(config, db, blobs, clock)
    services: Services = app.state.services
    services.readiness.migrated = True
    logger.info(
        "control_plane_started",
        sqlite_version=sqlite3.sqlite_version,
        migrations_applied=applied,
        stale_uploads_swept=len(swept),
        data_dir=str(data_dir),
        config_fingerprint=config_fingerprint(config),
        auth_enabled=auth_enabled(config),
    )
    if not auth_enabled(config):
        logger.warning(
            "auth_disabled",
            detail="no RTLFARM_CLIENT_TOKEN or RTLFARM_WORKER_TOKEN is set; "
            "every route is open to anyone who can reach the bind address",
        )
    return ControlPlane(config, app, db, blobs)
