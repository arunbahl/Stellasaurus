"""HotStateStore freshness logic with an injected clock."""

from stellasaurus.common.types import OutcomePolarity, PairSource, PairStatus, Venue
from stellasaurus.hot_path.book import NativeBook, PriceLevel
from stellasaurus.hot_path.normalize import normalize
from stellasaurus.hot_path.snapshot import (
    LimitsSnapshot,
    PairRegistryEntry,
    RegistrySnapshot,
)
from stellasaurus.hot_path.state import HotStateStore


class FakeClock:
    def __init__(self) -> None:
        self.t = 0

    def mono_ns(self) -> int:
        return self.t

    def wall_ms(self) -> int:
        return self.t // 1_000_000


def _limits() -> LimitsSnapshot:
    return LimitsSnapshot(1, True, 0, 0.0, 1, 1, 1, 1, 1, 1, 0.5)


def _book(venue: Venue, recv_mono_ns: int) -> "object":
    nb = NativeBook(venue, "x", (PriceLevel(500_000, 1),), (PriceLevel(510_000, 1),),
                    None, None, 1, recv_mono_ns, 0)
    return normalize(nb, polarity=OutcomePolarity.DIRECT, pair_id="p")


def _store(clock: FakeClock) -> HotStateStore:
    entry = PairRegistryEntry(
        "p", "prop", "KX", "slug", OutcomePolarity.DIRECT, PairStatus.VERIFIED,
        None, None, 0, "fp", PairSource.MANUAL_SEED,
    )
    return HotStateStore(
        registry=RegistrySnapshot.build(1, [entry]),
        limits=_limits(),
        book_staleness_ms=2000,
        clock=clock,
    )


def test_not_fresh_when_a_leg_missing():
    clock = FakeClock()
    store = _store(clock)
    store.publish_book(_book(Venue.KALSHI, recv_mono_ns=0))
    # only one leg present -> not fresh
    assert store.is_fresh("p") is False


def test_fresh_when_both_legs_recent():
    clock = FakeClock()
    store = _store(clock)
    store.publish_book(_book(Venue.KALSHI, recv_mono_ns=0))
    store.publish_book(_book(Venue.POLYMARKET, recv_mono_ns=0))
    clock.t = 1_000 * 1_000_000  # 1000 ms later, within 2000 ms threshold
    assert store.is_fresh("p") is True


def test_stale_when_a_leg_too_old():
    clock = FakeClock()
    store = _store(clock)
    store.publish_book(_book(Venue.KALSHI, recv_mono_ns=0))
    store.publish_book(_book(Venue.POLYMARKET, recv_mono_ns=0))
    clock.t = 3_000 * 1_000_000  # 3000 ms later, exceeds 2000 ms threshold
    assert store.is_fresh("p") is False
