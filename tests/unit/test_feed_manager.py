"""FeedManager: feeds start/stop in response to registry changes."""

import asyncio
from dataclasses import dataclass, field

from stellasaurus.background.feed_manager import FeedManager
from stellasaurus.common.types import OutcomePolarity, PairSource, PairStatus, Venue
from stellasaurus.hot_path.snapshot import (
    LimitsSnapshot,
    PairRegistryEntry,
    RegistrySnapshot,
)
from stellasaurus.hot_path.state import HotStateStore

FUTURE = 1_999_000_000_000


def _entry(pid: str, k: str, p: str) -> PairRegistryEntry:
    return PairRegistryEntry(
        pid, "prop", k, p, OutcomePolarity.DIRECT, PairStatus.VERIFIED,
        FUTURE, None, 0, "fp", PairSource.LLM,
    )


@dataclass
class FakeStats:
    venue: Venue = Venue.KALSHI


@dataclass
class FakeFeed:
    stats: FakeStats = field(default_factory=FakeStats)


@dataclass
class FakePlanned:
    feed: FakeFeed
    runner: object


class FakeSubMgr:
    """Each plan() returns one long-running fake feed; records lifecycle."""

    def __init__(self) -> None:
        self.plan_calls = 0
        self.started: list[int] = []
        self.cancelled: list[int] = []

    def plan(self):
        self.plan_calls += 1
        gen = self.plan_calls

        async def runner() -> None:
            self.started.append(gen)
            try:
                await asyncio.Event().wait()  # run until cancelled
            except asyncio.CancelledError:
                self.cancelled.append(gen)
                raise

        return [FakePlanned(feed=FakeFeed(), runner=runner)]


def _store(entries: list[PairRegistryEntry]) -> HotStateStore:
    return HotStateStore(
        registry=RegistrySnapshot.build(1, entries, now_ms=0),
        limits=LimitsSnapshot(1, True, 0, 0.0, 1, 1, 1, 1, 1, 1, 0.5),
        book_staleness_ms=2000,
    )


async def test_feeds_start_when_pairs_appear_and_replan_on_change():
    store = _store([])
    sub = FakeSubMgr()
    mgr = FeedManager(store=store, sub_mgr=sub, check_interval_s=0.01)
    task = asyncio.create_task(mgr.run())
    try:
        await asyncio.sleep(0.05)
        assert sub.plan_calls == 0  # empty registry -> no feeds

        # a pair gets verified -> feeds must start without restart
        store.publish_registry(RegistrySnapshot.build(2, [_entry("p1", "K1", "S1")], now_ms=0))
        await asyncio.sleep(0.05)
        assert sub.plan_calls == 1
        assert sub.started == [1]

        # the verified set changes -> old feed cancelled, new plan started
        entries = [_entry("p1", "K1", "S1"), _entry("p2", "K2", "S2")]
        store.publish_registry(RegistrySnapshot.build(3, entries, now_ms=0))
        await asyncio.sleep(0.05)
        assert sub.plan_calls == 2
        assert sub.cancelled == [1]
        assert sub.started == [1, 2]

        # unchanged set -> no re-plan
        await asyncio.sleep(0.05)
        assert sub.plan_calls == 2
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    # manager teardown cancelled the live feed
    assert sub.cancelled == [1, 2]


async def test_feeds_stop_when_registry_empties():
    store = _store([_entry("p1", "K1", "S1")])
    sub = FakeSubMgr()
    mgr = FeedManager(store=store, sub_mgr=sub, check_interval_s=0.01)
    task = asyncio.create_task(mgr.run())
    try:
        await asyncio.sleep(0.05)
        assert sub.started == [1]
        store.publish_registry(RegistrySnapshot.build(2, [], now_ms=0))
        await asyncio.sleep(0.05)
        assert sub.cancelled == [1]
        assert sub.plan_calls == 1  # empty set -> feeds stopped, no new plan
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


# --- Item 1a: multi-pair routing + re-normalize on polarity change ---

def _sub_mgr(store):
    import httpx

    from stellasaurus.background.subscription_mgr import SubscriptionManager
    from stellasaurus.common.config import Settings
    from stellasaurus.hot_path.ingest import BookStore
    return SubscriptionManager(
        settings=Settings(), http=httpx.AsyncClient(),
        store=store, book_store=BookStore(store),
    )


def _native(native_id: str):
    from stellasaurus.hot_path.book import NativeBook, PriceLevel
    # Poly-style single YES book: yes bids/asks present, no_* None (derived)
    return NativeBook(
        Venue.POLYMARKET, native_id,
        (PriceLevel(600_000, 100),), (PriceLevel(650_000, 100),),
        None, None, 1, 0, 0,
    )


def _store_with(entries):
    return HotStateStore(
        registry=RegistrySnapshot.build(1, entries, now_ms=0),
        limits=LimitsSnapshot(1, False, 0, 0.0, 1, 10**8, 10**9, 10**12, 20,
                              10**12, 0.5),
        book_staleness_ms=10**9,
    )


def test_one_native_market_routes_to_both_pairs_opposite_polarity():
    """UFC-style: two pairs share one Poly market with OPPOSITE polarity; a
    single native frame must normalize once per pair, each its own way."""
    e_direct = PairRegistryEntry("pd", "p", "KHOL", "polyslug", OutcomePolarity.DIRECT,
                                 PairStatus.VERIFIED, FUTURE, None, 0, "fp", PairSource.STRUCTURED)
    e_inv = PairRegistryEntry("pi", "p", "KMCG", "polyslug", OutcomePolarity.INVERTED,
                              PairStatus.VERIFIED, FUTURE, None, 0, "fp", PairSource.STRUCTURED)
    store = _store_with([e_direct, e_inv])
    sm = _sub_mgr(store)
    sm._build_maps()
    sm._on_book(Venue.POLYMARKET)(_native("polyslug"))
    bd = store.book("pd", Venue.POLYMARKET)
    bi = store.book("pi", Venue.POLYMARKET)
    assert bd is not None and bi is not None  # BOTH pairs got a book
    # DIRECT vs INVERTED must produce different canonical-YES books
    assert bd.yes_asks != bi.yes_asks


async def test_polarity_change_renormalizes_retained_native_book():
    """A DIRECT->INVERTED correction must re-normalize the CACHED book with no
    new frame (the stale-normalization bug)."""
    e = PairRegistryEntry("p1", "p", "K1", "s1", OutcomePolarity.DIRECT,
                          PairStatus.VERIFIED, FUTURE, None, 0, "fp", PairSource.STRUCTURED)
    store = _store_with([e])
    sm = _sub_mgr(store)
    sm._build_maps()
    sm._on_book(Venue.POLYMARKET)(_native("s1"))
    before = store.book("p1", Venue.POLYMARKET)
    # flip the pair to INVERTED and publish the new snapshot
    e2 = PairRegistryEntry("p1", "p", "K1", "s1", OutcomePolarity.INVERTED,
                           PairStatus.VERIFIED, FUTURE, None, 0, "fp", PairSource.STRUCTURED)
    store.publish_registry(RegistrySnapshot.build(2, [e2], now_ms=0))
    sm.refresh_routes()  # no new frame — must re-normalize from the retained native
    after = store.book("p1", Venue.POLYMARKET)
    assert after is not None and after.yes_asks != before.yes_asks


def test_kalshi_leg_always_direct_even_for_inverted_pair():
    """outcome_polarity is Poly-relative-to-Kalshi; the Kalshi (reference) book
    must NOT be reflected for an INVERTED pair (that phantom'd every sports edge)."""
    from stellasaurus.hot_path.book import NativeBook, PriceLevel
    e = PairRegistryEntry("p", "prop", "KTICK", "pslug", OutcomePolarity.INVERTED,
                          PairStatus.VERIFIED, FUTURE, None, 0, "fp", PairSource.LLM)
    store = _store_with([e])
    sm = _sub_mgr(store)
    sm._build_maps()
    # Kalshi native: two-sided book (bids 0.40 / asks 0.42)
    kn = NativeBook(Venue.KALSHI, "KTICK",
                    (PriceLevel(400_000, 10),), (PriceLevel(420_000, 10),),
                    None, None, 1, 0, 0)
    sm._on_book(Venue.KALSHI)(kn)
    book = store.book("p", Venue.KALSHI)
    # DIRECT normalization keeps the native YES ask as canonical YES ask (0.42),
    # NOT reflected to ~0.60 as an INVERTED mapping would.
    assert book is not None and book.yes_asks and book.yes_asks[0].price == 420_000
