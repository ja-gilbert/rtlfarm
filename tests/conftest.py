"""Shared pytest configuration.

Hypothesis is derandomized so a property failure in CI reproduces on the next
run. The autouse fixtures strip ambient ``RTLFARM_*`` variables so a test sees
only what it sets, and put the root logger back, since ``configure_logging``
replaces its handlers and a test that fails halfway would leak one into the
rest of the session.
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
