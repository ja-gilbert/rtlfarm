"""Named crash points for fault injection.

``point("name")`` does nothing unless ``RTLFARM_TEST_HOOKS=1`` and a test
harness has armed an action for that name (``raise`` in the fast tier,
``os._exit(1)`` in the process tier).

The Core set is closed so a coverage test can assert, once the scheduler and
the claim exist, that every member fires somewhere in the suite (spec §20.2).
The name is checked on every call, enabled or not, so a misspelled hook
fails loudly instead of silently never firing.
"""

from __future__ import annotations

import os
from collections.abc import Callable

ENV_VAR = "RTLFARM_TEST_HOOKS"

HOOK_NAMES: frozenset[str] = frozenset(
    {
        "after_claim_before_response",
        "after_commit_before_response",
        "after_blob_rename_before_row",
        "after_task_terminal_before_job_aggregate",
        "between_tick_steps",
        "before_expiry_commit",
    }
)


class UnknownHook(ValueError):
    """A hook name outside ``HOOK_NAMES``."""


_armed: dict[str, Callable[[], None]] = {}


def _check_name(name: str) -> None:
    if name not in HOOK_NAMES:
        raise UnknownHook(f"unknown hook {name!r}; known: {sorted(HOOK_NAMES)}")


def enabled() -> bool:
    return os.environ.get(ENV_VAR) == "1"


def point(name: str) -> None:
    """Run the action armed for ``name``, if hooks are enabled and one is armed."""
    _check_name(name)
    if not enabled():
        return
    action = _armed.get(name)
    if action is not None:
        action()


def arm(name: str, action: Callable[[], None]) -> None:
    """Make every later ``point(name)`` run ``action`` (only while enabled).

    Arming is sticky until ``disarm_all``; a harness that wants a one-shot
    crash disarms inside the action.
    """
    _check_name(name)
    _armed[name] = action


def disarm_all() -> None:
    _armed.clear()
