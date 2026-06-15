"""Time sources, injectable for deterministic tests.

Monotonic time is used for latency/staleness (never affected by wall-clock
adjustments); wall time is used only for display and persistence timestamps.
"""

from __future__ import annotations

import time
from typing import Protocol


class Clock(Protocol):
    def mono_ns(self) -> int:
        """Monotonic nanoseconds — for latency and staleness only."""
        ...

    def wall_ms(self) -> int:
        """Wall-clock epoch milliseconds — for display/persistence."""
        ...


class SystemClock:
    def mono_ns(self) -> int:
        return time.monotonic_ns()

    def wall_ms(self) -> int:
        return time.time_ns() // 1_000_000


SYSTEM_CLOCK = SystemClock()


def mono_ns() -> int:
    return SYSTEM_CLOCK.mono_ns()


def wall_ms() -> int:
    return SYSTEM_CLOCK.wall_ms()
