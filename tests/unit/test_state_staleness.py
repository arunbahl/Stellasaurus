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


def test_quiet_book_on_live_feed_stays_fresh():
    """Delta feeds only push on change: an old book is fresh while ANY frame
    from its venue is recent."""
    clock = FakeClock()
    store = _store(clock)
    store.publish_book(_book(Venue.KALSHI, recv_mono_ns=0))
    store.publish_book(_book(Venue.POLYMARKET, recv_mono_ns=0))
    clock.t = 10_000 * 1_000_000  # both books now 10s old (threshold 2s)
    assert store.is_fresh("p") is False  # venues quiet too -> stale
    # a frame for ANOTHER market arrives on each venue -> feeds alive
    for venue in (Venue.KALSHI, Venue.POLYMARKET):
        other = NativeBook(venue, "other", (PriceLevel(500_000, 1),),
                           (PriceLevel(510_000, 1),), None, None, 2,
                           clock.t - 1_000_000, 0)
        store.publish_book(normalize(other, polarity=OutcomePolarity.DIRECT,
                                     pair_id="other-pair"))
    assert store.is_fresh("p") is True  # quiet book, live feed -> fresh


def test_frozen_book_is_stale_beyond_max_quiet_even_on_live_feed():
    """A settled/resolved market's book freezes; a still-live feed would keep it
    'fresh' forever, manufacturing a post-match phantom. The max-quiet bound
    marks a long-frozen book stale despite the venue being alive."""
    clock = FakeClock()
    store = HotStateStore(
        registry=RegistrySnapshot.build(1, [PairRegistryEntry(
            "p", "prop", "KX", "slug", OutcomePolarity.DIRECT, PairStatus.VERIFIED,
            None, None, 0, "fp", PairSource.MANUAL_SEED)]),
        limits=_limits(), book_staleness_ms=2000, book_max_quiet_ms=600_000, clock=clock,
    )
    store.publish_book(_book(Venue.KALSHI, recv_mono_ns=0))
    store.publish_book(_book(Venue.POLYMARKET, recv_mono_ns=0))
    # keep BOTH feeds demonstrably alive with fresh frames for OTHER markets...
    clock.t = 700_000 * 1_000_000  # 700s later (> 600s max_quiet)
    for v in (Venue.KALSHI, Venue.POLYMARKET):
        other = NativeBook(v, "other", (PriceLevel(500_000, 1),), (PriceLevel(510_000, 1),),
                           None, None, 2, clock.t - 1_000_000, 0)
        store.publish_book(normalize(other, polarity=OutcomePolarity.DIRECT, pair_id="other"))
    # ...but pair "p"'s own books are frozen at t=0 -> beyond max_quiet -> stale
    assert store.is_fresh("p") is False
