"""The injected clock."""

from __future__ import annotations

import asyncio
import time

import pytest

from rtlfarm.clock import Clock, DrivenClock, WallClock


def test_driven_clock_starts_where_told() -> None:
    assert DrivenClock(start_ms=1_000).now_ms() == 1_000


def test_driven_clock_defaults_to_zero() -> None:
    assert DrivenClock().now_ms() == 0


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


def test_driven_clock_rejects_a_negative_sleep() -> None:
    with pytest.raises(ValueError, match="negative"):
        asyncio.run(DrivenClock().sleep(-0.1))


def test_wall_clock_rejects_a_negative_sleep() -> None:
    with pytest.raises(ValueError, match="negative"):
        asyncio.run(WallClock().sleep(-0.1))


def test_wall_clock_tracks_real_time_in_milliseconds() -> None:
    clock = WallClock()
    now = clock.now_ms()
    assert abs(now - time.time() * 1000) < 2_000
    assert isinstance(now, int)


def test_wall_clock_sleep_really_waits() -> None:
    clock = WallClock()
    before = time.monotonic()
    asyncio.run(clock.sleep(0.05))
    assert time.monotonic() - before >= 0.045


def test_both_clocks_satisfy_the_protocol() -> None:
    clocks: list[Clock] = [WallClock(), DrivenClock()]
    for clock in clocks:
        assert clock.now_ms() >= 0
