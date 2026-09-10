"""Structured JSON logging: the line shape log shipping depends on, tracebacks
on error, and who owns the root handlers."""

from __future__ import annotations

import io
import json
import logging
import sys
from datetime import datetime, timedelta

import pytest

from rtlfarm import log


@pytest.fixture
def stream() -> io.StringIO:
    return io.StringIO()


@pytest.fixture
def logger(stream: io.StringIO) -> log.EventLogger:
    log.configure_logging(service="control", stream=stream, level=logging.INFO)
    return log.get_logger("rtlfarm.test")


def _only_line(stream: io.StringIO) -> dict[str, object]:
    lines = stream.getvalue().splitlines()
    assert len(lines) == 1, lines
    parsed: dict[str, object] = json.loads(lines[0])
    return parsed


def test_every_line_carries_the_fixed_fields(
    logger: log.EventLogger, stream: io.StringIO
) -> None:
    logger.info("task_claimed", task_id="t1", slots=2)
    record = _only_line(stream)
    assert set(record) >= {
        "ts",
        "level",
        "service",
        "event",
        "job_id",
        "task_id",
        "attempt_id",
        "worker_id",
        "request_id",
    }
    assert record["level"] == "INFO"
    assert record["service"] == "control"
    assert record["event"] == "task_claimed"
    assert record["task_id"] == "t1"
    assert record["job_id"] is None
    assert record["slots"] == 2
    ts = record["ts"]
    assert isinstance(ts, str)
    assert datetime.fromisoformat(ts).utcoffset() == timedelta(0)


def test_configure_logging_owns_the_root_handlers(stream: io.StringIO) -> None:
    """A handler installed by someone else would print every event a second time."""
    foreign_stream = io.StringIO()
    foreign = logging.StreamHandler(foreign_stream)
    logging.getLogger().addHandler(foreign)
    try:
        log.configure_logging(service="control", stream=stream, level=logging.INFO)
        log.get_logger("rtlfarm.test").info("started")
    finally:
        logging.getLogger().removeHandler(foreign)
    assert foreign_stream.getvalue() == ""
    assert _only_line(stream)["event"] == "started"


def test_exception_logs_carry_the_traceback(
    logger: log.EventLogger, stream: io.StringIO
) -> None:
    try:
        raise ZeroDivisionError("boom")
    except ZeroDivisionError:
        logger.exception("tick_step_failed", step="expiry")
    record = _only_line(stream)
    assert record["level"] == "ERROR"
    assert record["step"] == "expiry"
    exc = record["exc"]
    assert isinstance(exc, str)
    assert "ZeroDivisionError" in exc
    assert "Traceback" in exc


def test_default_stream_is_resolved_at_call_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stdout swapped in after import (as pytest does) still receives events.

    Regression: the stream was bound at import time.
    """
    replacement = io.StringIO()
    monkeypatch.setattr(sys, "stdout", replacement)
    log.configure_logging("svc")
    log.get_logger("t").info("hello")
    assert json.loads(replacement.getvalue())["event"] == "hello"
