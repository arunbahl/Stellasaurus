"""Auto-flattener: retries until the venue reports flat; gives up loudly."""

from stellasaurus.background.flattener import NakedLeg, PositionFlattener
from stellasaurus.common.clock import Clock
from stellasaurus.common.types import Venue


class FakeClock(Clock):
    def mono_ns(self) -> int: return 0
    def wall_ms(self) -> int: return 0


class FakeCloser:
    """close_position returns the residual; scripted to flatten after N tries."""
    def __init__(self, flat_after: int):
        self.flat_after = flat_after
        self.calls = 0

    async def net_position(self, native_id): return 0 if self.calls >= self.flat_after else 1
    async def close_position(self, native_id):
        self.calls += 1
        return 0 if self.calls >= self.flat_after else 1


async def test_flatten_succeeds_after_retries():
    gw = FakeCloser(flat_after=3)
    f = PositionFlattener(gateways={Venue.KALSHI: gw}, max_attempts=8,
                          backoff_seconds=0, clock=FakeClock())
    ok = await f.flatten(NakedLeg(Venue.KALSHI, "KX"))
    assert ok is True
    assert gw.calls == 3


async def test_flatten_gives_up_after_budget():
    gw = FakeCloser(flat_after=99)  # never flattens
    f = PositionFlattener(gateways={Venue.KALSHI: gw}, max_attempts=4,
                          backoff_seconds=0, clock=FakeClock())
    ok = await f.flatten(NakedLeg(Venue.KALSHI, "KX"))
    assert ok is False
    assert gw.calls == 4  # exactly the budget, then stops (still halted upstream)
