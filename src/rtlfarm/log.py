"""Structured JSON logging to stdout.

One JSON object per line with a fixed shape — ``ts, level, service, event``
and the five correlation ids, present on every line (``null`` when the event
has none) — followed by the event's own fields, and ``exc`` with the
traceback when an exception is being logged. Tool stdout and stderr are
artifacts, never service logs.

``configure_logging`` owns the root logger: it removes every other handler so
each event is printed exactly once. Libraries that install handlers on their
own loggers with ``propagate=False`` (uvicorn does) must be told not to, e.g.
``uvicorn.run(..., log_config=None)``.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import TextIO

CORRELATION_IDS = ("job_id", "task_id", "attempt_id", "worker_id", "request_id")
FIXED_KEYS = frozenset({"ts", "level", "service", "event", "exc"})


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str) -> None:
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        fields: dict[str, object] = getattr(record, "fields", {})
        line: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "service": self.service,
            "event": record.getMessage(),
            **dict.fromkeys(CORRELATION_IDS),
            **fields,
        }
        if record.exc_info:
            line["exc"] = self.formatException(record.exc_info)
        return json.dumps(line, default=str)


class EventLogger:
    """``logger.info("event_name", key=value, ...)`` — an event plus fields."""

    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger

    def _log(
        self, level: int, event: str, fields: dict[str, object], exc_info: bool = False
    ) -> None:
        reserved = FIXED_KEYS.intersection(fields)
        if reserved:
            raise ValueError(f"log fields {sorted(reserved)} are reserved")
        self._logger.log(level, event, extra={"fields": fields}, exc_info=exc_info)

    def debug(self, event: str, **fields: object) -> None:
        self._log(logging.DEBUG, event, fields)

    def info(self, event: str, **fields: object) -> None:
        self._log(logging.INFO, event, fields)

    def warning(self, event: str, **fields: object) -> None:
        self._log(logging.WARNING, event, fields)

    def error(self, event: str, **fields: object) -> None:
        self._log(logging.ERROR, event, fields)

    def exception(self, event: str, **fields: object) -> None:
        """An ERROR event carrying the traceback of the exception being handled."""
        self._log(logging.ERROR, event, fields, exc_info=True)


def configure_logging(
    service: str, *, stream: TextIO | None = None, level: int = logging.INFO
) -> None:
    """Make the JSON handler the root logger's only handler.

    ``stream`` defaults to the process's stdout *at call time*, not at import
    time, so a caller that has redirected stdout (a test harness, say) sees
    the events.
    """
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    handler = logging.StreamHandler(stream if stream is not None else sys.stdout)
    handler.setFormatter(JsonFormatter(service))
    root.addHandler(handler)
    root.setLevel(level)


def get_logger(name: str) -> EventLogger:
    return EventLogger(logging.getLogger(name))
