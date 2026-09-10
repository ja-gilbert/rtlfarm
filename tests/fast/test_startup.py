"""Bringing the control plane up: the data directory, migrations, the stale
upload sweep, readiness, the startup log line with its auth banner, and the
failures that must still close the database.
"""

from __future__ import annotations

import io
import json
import logging
import os
import sqlite3
from pathlib import Path

import httpx
import pytest

from rtlfarm import log
from rtlfarm.clock import DrivenClock
from rtlfarm.config import Config, TimingConfig
from rtlfarm.control import startup
from rtlfarm.control.startup import (
    StartupError,
    config_fingerprint,
    open_database,
    prepare,
)

################################################################################
# Helpers
################################################################################


@pytest.fixture(autouse=True)
def stream() -> io.StringIO:
    """Every test here logs into a buffer of its own."""
    buffer = io.StringIO()
    log.configure_logging("control", stream=buffer, level=logging.INFO)
    return buffer


def _events(stream: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def _config(
    tmp_path: Path,
    *,
    client_token: str | None = None,
    worker_token: str | None = None,
    timing: TimingConfig | None = None,
) -> Config:
    return Config(
        data_dir=str(tmp_path / "data"),
        client_token=client_token,
        worker_token=worker_token,
        timing=timing if timing is not None else TimingConfig(),
    )


################################################################################
# prepare()
################################################################################


async def test_prepare_creates_the_volume_layout_and_migrates(
    tmp_path: Path, clock: DrivenClock
) -> None:
    plane = prepare(_config(tmp_path, client_token="c"), clock, synchronous="OFF")
    try:
        data = tmp_path / "data"
        assert (data / startup.DB_FILENAME).is_file()
        for name in ("sha256", "tmp", "trash"):
            assert (data / startup.BLOBS_DIRNAME / name).is_dir()
        reader = plane.db.read()
        try:
            versions = reader.execute(
                "SELECT version FROM schema_migrations"
            ).fetchall()
        finally:
            reader.close()
        assert versions == [(1,)]
        transport = httpx.ASGITransport(app=plane.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://x") as c:
            ready = await c.get("/readyz")
        assert ready.status_code == 200
        assert ready.json()["checks"]["migrated"] is True
    finally:
        plane.close()


def test_prepare_is_idempotent_across_restarts(
    tmp_path: Path, clock: DrivenClock, stream: io.StringIO
) -> None:
    config = _config(tmp_path, client_token="c")
    first = prepare(config, clock, synchronous="OFF")
    first.close()
    second = prepare(config, clock, synchronous="OFF")
    second.close()
    started = [e for e in _events(stream) if e["event"] == "control_plane_started"]
    assert [e["migrations_applied"] for e in started] == [[1], []]


def test_prepare_sweeps_only_stale_uploads(
    tmp_path: Path, clock: DrivenClock, stream: io.StringIO
) -> None:
    """The boundary is the configured upload timeout, not any other constant."""
    timing = TimingConfig(upload_timeout_s=60)
    tmp = tmp_path / "data" / startup.BLOBS_DIRNAME / "tmp"
    tmp.mkdir(parents=True)
    stale, fresh = tmp / "stale", tmp / "fresh"
    stale.write_bytes(b"old")
    fresh.write_bytes(b"new")
    now_s = clock.now_ms() / 1000
    os.utime(stale, (now_s - 61, now_s - 61))
    os.utime(fresh, (now_s - 59, now_s - 59))
    config = _config(tmp_path, client_token="c", timing=timing)
    prepare(config, clock, synchronous="OFF").close()
    assert not stale.exists()
    assert fresh.exists()
    (started,) = [e for e in _events(stream) if e["event"] == "control_plane_started"]
    assert started["stale_uploads_swept"] == 1


def test_startup_line_reports_the_environment_without_secrets(
    tmp_path: Path, clock: DrivenClock, stream: io.StringIO
) -> None:
    config = _config(tmp_path, client_token="secret-c", worker_token="secret-w")
    prepare(config, clock, synchronous="OFF").close()
    text = stream.getvalue()
    assert "secret-c" not in text
    assert "secret-w" not in text
    (started,) = [e for e in _events(stream) if e["event"] == "control_plane_started"]
    assert started["service"] == "control"
    assert started["auth_enabled"] is True
    assert str(started["sqlite_version"]).count(".") == 2
    assert started["config_fingerprint"] == config_fingerprint(config)
    assert "auth_disabled" not in [e["event"] for e in _events(stream)]


def test_auth_disabled_logs_a_warning_banner(
    tmp_path: Path, clock: DrivenClock, stream: io.StringIO
) -> None:
    prepare(_config(tmp_path), clock, synchronous="OFF").close()
    (banner,) = [e for e in _events(stream) if e["event"] == "auth_disabled"]
    assert banner["level"] == "WARNING"
    assert "open" in str(banner["detail"])


################################################################################
# Failure Before Serving
################################################################################


def test_a_file_that_is_not_a_database_is_a_startup_error(
    tmp_path: Path, clock: DrivenClock
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    (data / startup.DB_FILENAME).write_bytes(b"not a database" * 8)
    with pytest.raises(StartupError, match=startup.DB_FILENAME):
        open_database(data, clock, synchronous="OFF")


def test_a_migration_failure_is_a_startup_error_and_closes_the_writer(
    tmp_path: Path, clock: DrivenClock
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    seeded = sqlite3.connect(data / startup.DB_FILENAME)
    seeded.execute("CREATE TABLE schema_migrations (wrong INTEGER)")
    seeded.close()
    with pytest.raises(StartupError, match=startup.DB_FILENAME):
        open_database(data, clock, synchronous="OFF")
    assert not (data / f"{startup.DB_FILENAME}-wal").exists()


def test_a_failure_after_the_database_opened_closes_it(
    tmp_path: Path, clock: DrivenClock
) -> None:
    """A blob directory that is a file fails ``prepare`` after the migration;
    the writer connection must not outlive the failure (a live WAL would)."""
    data = tmp_path / "data"
    data.mkdir()
    (data / startup.BLOBS_DIRNAME).write_text("", "utf-8")
    with pytest.raises(StartupError, match=startup.BLOBS_DIRNAME):
        prepare(_config(tmp_path, client_token="c"), clock, synchronous="OFF")
    assert (data / startup.DB_FILENAME).is_file()
    assert not (data / f"{startup.DB_FILENAME}-wal").exists()


################################################################################
# The Configuration Fingerprint
################################################################################


def test_fingerprint_reflects_token_presence_but_never_token_values() -> None:
    """A fingerprint over the token values would let a log reader compare
    them; one blind to their presence would hide an auth change."""
    a = Config(client_token="one", worker_token="x", data_dir="d")
    b = Config(client_token="two", worker_token="y", data_dir="d")
    assert config_fingerprint(a) == config_fingerprint(b)
    assert config_fingerprint(Config(client_token="c")) != config_fingerprint(Config())
    with_worker = Config(client_token="one", worker_token="x")
    assert config_fingerprint(with_worker) != config_fingerprint(
        Config(client_token="one")
    )
