"""The injected clock.

The control plane never calls ``time.time()`` directly. It receives a
``Clock``; production injects ``WallClock`` and the fast test tier injects
``DrivenClock`` so that "the lease expired" is a decision the test makes with
``advance()``, not a race it waits for.
"""

from __future__ import annotations

import asyncio
import time
from typing import Protocol


class Clock(Protocol):
    def now_ms(self) -> int:
        """Milliseconds since the Unix epoch."""
        ...

    async def sleep(self, seconds: float) -> None:
        """Wait ``seconds`` of this clock's time without blocking the event loop."""
        ...


def _check_duration(seconds: float) -> None:
    if seconds < 0:
        raise ValueError(f"cannot sleep a negative duration ({seconds} s)")


class WallClock:
    """The real clock."""

    def now_ms(self) -> int:
        return time.time_ns() // 1_000_000

    async def sleep(self, seconds: float) -> None:
        _check_duration(seconds)
        await asyncio.sleep(seconds)


class DrivenClock:
    """A clock that moves only when a test moves it.

    Time moves through ``advance`` and nothing else, so "the lease expired" is
    always a decision the test made. ``sleep`` therefore does not move the
    clock: it yields to the event loop once and returns, which keeps a loop
    that awaits it from expiring leases on its own. The fast tier never runs
    the scheduler's loop anyway; it calls ``tick()`` directly.

    ``advance`` accepts a negative step so a test can model a wall clock that
    went backwards (the scheduler skips lease expiry on a backward step).
    """

    def __init__(self, start_ms: int = 0) -> None:
        self._now_ms = start_ms

    def now_ms(self) -> int:
        return self._now_ms

    def advance(self, ms: int) -> None:
        self._now_ms += ms

    async def sleep(self, seconds: float) -> None:
        _check_duration(seconds)
        await asyncio.sleep(0)
