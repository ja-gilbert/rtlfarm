"""Structured JSON logging: the line shape, the level threshold, and who owns
the root handlers."""

from __future__ import annotations

import io
import json
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

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


@pytest.mark.parametrize(
    ("threshold", "emitted"),
    [(logging.DEBUG, ["DEBUG", "INFO"]), (logging.WARNING, [])],
    ids=["debug-threshold-emits-debug-and-info", "warning-threshold-drops-both"],
)
def test_the_configured_level_is_the_threshold(
    stream: io.StringIO, threshold: int, emitted: list[str]
) -> None:
    """The level given to configure_logging decides what is emitted, in both
    directions; the root logger's own default (WARNING) does not."""
    log.configure_logging(service="control", stream=stream, level=threshold)
    logger = log.get_logger("rtlfarm.test")
    logger.debug("noise")
    logger.info("started")
    levels = [json.loads(line)["level"] for line in stream.getvalue().splitlines()]
    assert levels == emitted


def test_warning_and_error_levels(logger: log.EventLogger, stream: io.StringIO) -> None:
    logger.warning("lease_expired", attempt_id="a1")
    logger.error("tick_step_failed", step="expiry")
    levels = [json.loads(line)["level"] for line in stream.getvalue().splitlines()]
    assert levels == ["WARNING", "ERROR"]


def test_one_json_object_per_line_even_with_newlines_in_fields(
    logger: log.EventLogger, stream: io.StringIO
) -> None:
    logger.info("tool_output", detail="line one\nline two")
    assert _only_line(stream)["detail"] == "line one\nline two"


def test_non_json_values_fall_back_to_str(
    logger: log.EventLogger, stream: io.StringIO
) -> None:
    logger.info("blob_written", path=Path("/blobs/sha256/ab"))
    assert _only_line(stream)["path"] == "/blobs/sha256/ab"


def test_reconfiguring_replaces_the_previous_handler(stream: io.StringIO) -> None:
    other = io.StringIO()
    log.configure_logging(service="control", stream=other, level=logging.INFO)
    log.configure_logging(service="worker", stream=stream, level=logging.INFO)
    log.get_logger("rtlfarm.test").info("started")
    assert other.getvalue() == ""
    assert _only_line(stream)["service"] == "worker"


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


@pytest.mark.parametrize("name", ["ts", "level", "service", "exc"])
def test_fixed_keys_cannot_be_overwritten_by_fields(
    logger: log.EventLogger, stream: io.StringIO, name: str
) -> None:
    with pytest.raises(ValueError, match=name):
        logger.info("started", **{name: "overwritten"})
    assert stream.getvalue() == ""


def test_default_stream_is_resolved_at_call_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stdout swapped in after import (as pytest does) still receives events.

    Regression: the stream was bound at import time until 0fd4037 (RC-04).
    """
    replacement = io.StringIO()
    monkeypatch.setattr(sys, "stdout", replacement)
    log.configure_logging("svc")
    log.get_logger("t").info("hello")
    assert json.loads(replacement.getvalue())["event"] == "hello"
