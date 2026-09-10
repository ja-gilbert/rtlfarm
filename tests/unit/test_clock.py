"""The wall clock: every timestamp and lease in the schema is epoch
milliseconds, so the unit it returns is the one contract that matters."""

from __future__ import annotations

import time

from rtlfarm.clock import WallClock


def test_wall_clock_tracks_real_time_in_milliseconds() -> None:
    """A seconds or nanoseconds slip would put every lease off by a factor
    of a thousand or more."""
    clock = WallClock()
    now = clock.now_ms()
    assert abs(now - time.time() * 1000) < 2_000
    assert isinstance(now, int)
