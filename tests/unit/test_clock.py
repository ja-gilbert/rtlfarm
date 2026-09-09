"""The injected clock: the driven clock the fast tier moves by hand, and the
wall clock it stands in for."""

from __future__ import annotations

import asyncio
import time

import pytest

from rtlfarm.clock import Clock, DrivenClock, WallClock

################################################################################
# The Driven Clock
################################################################################


def test_driven_clock_advance_accumulates() -> None:
    clock = DrivenClock(start_ms=1_000)
    clock.advance(250)
    clock.advance(250)
    assert clock.now_ms() == 1_500


def test_driven_clock_does_not_move_on_its_own() -> None:
    clock = DrivenClock(start_ms=1_000)
    time.sleep(0.02)
    assert clock.now_ms() == 1_000


def test_driven_clock_can_model_a_backward_wall_clock() -> None:
    """The scheduler skips lease expiry when the clock steps back; model that."""
    clock = DrivenClock(start_ms=1_000)
    clock.advance(-400)
    assert clock.now_ms() == 600


def test_driven_clock_sleep_yields_without_moving_time() -> None:
    """Time moves only through advance(): a sleeping loop cannot expire a lease."""
    clock = DrivenClock(start_ms=1_000)
    before = time.monotonic()
    asyncio.run(clock.sleep(1.5))
    assert clock.now_ms() == 1_000
    assert time.monotonic() - before < 0.5


################################################################################
# Both Clocks
################################################################################


@pytest.mark.parametrize("clock", [DrivenClock(), WallClock()], ids=["driven", "wall"])
def test_sleep_rejects_a_negative_duration(clock: Clock) -> None:
    with pytest.raises(ValueError, match="negative"):
        asyncio.run(clock.sleep(-0.1))


################################################################################
# The Wall Clock
################################################################################


def test_wall_clock_tracks_real_time_in_milliseconds() -> None:
    clock = WallClock()
    now = clock.now_ms()
    assert abs(now - time.time() * 1000) < 2_000
    assert isinstance(now, int)


def test_wall_clock_sleep_really_waits() -> None:
    """A sleep that returned at once would spin the tick loop."""
    clock = WallClock()
    before = time.monotonic()
    asyncio.run(clock.sleep(0.05))
    assert time.monotonic() - before >= 0.045
