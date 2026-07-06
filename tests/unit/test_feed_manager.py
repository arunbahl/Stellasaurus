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
