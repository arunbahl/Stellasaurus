"""FeedManager — keeps market-data feeds in sync with the verified registry.

The subscription plan used to be computed once at startup, so pairs verified
later (by the pairing loop or catalog sync) appeared in the registry but never
streamed until a restart. This manager closes that seam: it watches the
registry snapshot and, whenever the set of needed native markets changes,
tears down the running feeds and starts a fresh plan.

Feed tasks are owned here (not by the global TaskSupervisor) precisely so they
can be cancelled and replaced as a group. Each feed still gets crash-restart
with backoff while it is the current plan.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from stellasaurus.background.subscription_mgr import SubscriptionManager
from stellasaurus.common.logging import get_logger
from stellasaurus.hot_path.state import HotStateStore

_log = get_logger("background.feed_manager")


class FeedManager:
    def __init__(
        self,
        *,
        store: HotStateStore,
        sub_mgr: SubscriptionManager,
        check_interval_s: float = 30.0,
    ) -> None:
        self._store = store
        self._sub_mgr = sub_mgr
        self._interval = check_interval_s
        self._tasks: list[asyncio.Task[None]] = []
        self._current: frozenset[tuple[str, str]] = frozenset()
        # Polarity-inclusive route signature: changes even when the market SET is
        # unchanged (a pair flipping DIRECT<->INVERTED), so a correction re-routes.
        self._route_sig: frozenset[tuple[str, str, str]] = frozenset()

    def _needed(self) -> frozenset[tuple[str, str]]:
        snap = self._store.registry()
        return frozenset(
            (snap.by_id[pid].kalshi_ticker, snap.by_id[pid].poly_market_slug)
            for pid in snap.verified
        )

    def _route_signature(self) -> frozenset[tuple[str, str, str]]:
        snap = self._store.registry()
        return frozenset(
            (snap.by_id[pid].kalshi_ticker, snap.by_id[pid].poly_market_slug,
             snap.by_id[pid].outcome_polarity.value)
            for pid in snap.verified
        )

    async def run(self) -> None:
        """Supervised forever-task: re-plan feeds when the needed SET changes;
        cheaply re-route + re-normalize when only polarity changes. Owned feed
        tasks are torn down if this task itself is cancelled."""
        try:
            while True:
                needed = self._needed()
                if needed != self._current:
                    await self._replan(needed)  # rebuilds maps via plan()
                    self._route_sig = self._route_signature()
                else:
                    sig = self._route_signature()
                    if sig != self._route_sig:
                        self._sub_mgr.refresh_routes()
                        self._route_sig = sig
                        _log.info("routes_refreshed", pairs=len(needed))
                await asyncio.sleep(self._interval)
        finally:
            await self._stop_feeds()

    async def _replan(self, needed: frozenset[tuple[str, str]]) -> None:
        added = len(needed - self._current)
        removed = len(self._current - needed)
        await self._stop_feeds()
        planned = self._sub_mgr.plan() if needed else []
        self._tasks = [
            asyncio.create_task(
                self._run_feed(p.runner, name=f"feed:{p.feed.stats.venue.value}"),
                name=f"feed:{p.feed.stats.venue.value}",
            )
            for p in planned
        ]
        self._current = needed
        _log.info(
            "feeds_replanned",
            pairs=len(needed), added=added, removed=removed, feeds=len(self._tasks),
        )

    async def _stop_feeds(self) -> None:
        for t in self._tasks:
            t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []

    @staticmethod
    async def _run_feed(runner: Callable[[], Awaitable[None]], *, name: str) -> None:
        """Crash-restart with backoff for one feed, until cancelled by a re-plan."""
        backoff = 1.0
        while True:
            try:
                await runner()
                _log.warning("feed_exited_restarting", feed=name)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                _log.warning("feed_crashed", feed=name, error=str(exc), backoff_s=backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    async def shutdown(self) -> None:
        await self._stop_feeds()
