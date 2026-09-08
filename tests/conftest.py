"""Shared pytest configuration.

Hypothesis runs derandomized so a property-test failure in CI is
reproducible on the next run rather than a one-off.

Two autouse fixtures keep tests independent of the developer's shell and of
each other. The ``RTLFARM_*`` variables a developer exports to run the farm
by hand (tokens, a pinned digest) are removed for the duration of every test,
so the entry points under test see only what the test sets. The root
logger's handlers and level are put back after every test, because
``configure_logging`` replaces them and a test that fails halfway would
otherwise leave its handler behind for the rest of the session.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator

import pytest
from hypothesis import settings

from rtlfarm.config import ENV_PREFIX

settings.register_profile("rtlfarm", derandomize=True, max_examples=200)
settings.load_profile("rtlfarm")


@pytest.fixture(autouse=True)
def _no_ambient_rtlfarm_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith(ENV_PREFIX):
            monkeypatch.delenv(name)


@pytest.fixture(autouse=True)
def _restore_root_logger() -> Iterator[None]:
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    for handler in list(root.handlers):
        if handler not in handlers:
            root.removeHandler(handler)
    for handler in handlers:
        if handler not in root.handlers:
            root.addHandler(handler)
    root.setLevel(level)
