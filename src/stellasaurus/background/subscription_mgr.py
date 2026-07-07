"""Plan and run market-data subscriptions for the VERIFIED registry pairs.

Reads the current registry snapshot, collects each venue's native ids, builds a
``native_id -> (pair_id, polarity)`` map, then runs one or more feeds whose
``on_book`` callback normalizes to canonical-YES and pushes into the BookStore.

Transport per venue is chosen by credentials (WS when authenticated, else REST
poll). WS subscriptions are sharded to respect per-venue connection limits.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx

from stellasaurus.common.config import Settings
from stellasaurus.common.logging import get_logger
from stellasaurus.common.types import OutcomePolarity, Venue
from stellasaurus.hot_path.book import NativeBook
from stellasaurus.hot_path.ingest import BookStore
from stellasaurus.hot_path.normalize import normalize
from stellasaurus.hot_path.state import HotStateStore
from stellasaurus.venues.base import FeedStats, MarketFeed, RestPollFeed
from stellasaurus.venues.kalshi.client import KalshiClient
from stellasaurus.venues.kalshi.stream import KalshiStream
from stellasaurus.venues.polymarket.client import PolymarketClient
from stellasaurus.venues.polymarket.stream import PolymarketStream
from stellasaurus.venues.sharding import shard
from stellasaurus.venues.signing import KalshiSigner, PolymarketSigner

_log = get_logger("background.subscription_mgr")


@dataclass
class PlannedFeed:
    feed: MarketFeed
    native_ids: list[str]
    runner: Callable[[], Awaitable[None]]


class SubscriptionManager:
    def __init__(
        self,
        *,
        settings: Settings,
        http: httpx.AsyncClient,
        store: HotStateStore,
        book_store: BookStore,
    ) -> None:
        self._settings = settings
        self._http = http
        self._store = store
        self._book_store = book_store
        # native_id -> LIST of (pair_id, polarity): one native market can back
        # several pairs (e.g. both sides of a game reference the same Polymarket
        # market with OPPOSITE polarity), so each native frame normalizes once
        # per referencing pair.
        self._maps: dict[Venue, dict[str, list[tuple[str, OutcomePolarity]]]] = {
            Venue.KALSHI: {},
            Venue.POLYMARKET: {},
        }
        # Last native book per (venue, native_id) — retained so a registry
        # polarity change can re-normalize immediately instead of waiting for
        # the next frame (quiet markets would otherwise stay mis-normalized).
        self._native: dict[Venue, dict[str, NativeBook]] = {
            Venue.KALSHI: {},
            Venue.POLYMARKET: {},
        }
        self._feeds: list[MarketFeed] = []

    def feed_stats(self) -> list[FeedStats]:
        return [f.stats for f in self._feeds]

    def _build_maps(self) -> tuple[list[str], list[str]]:
        """(Re)build native_id -> [(pair_id, polarity)] routes from the current
        registry. Returns the distinct native ids per venue."""
        snapshot = self._store.registry()
        self._maps[Venue.KALSHI] = {}
        self._maps[Venue.POLYMARKET] = {}
        for pair_id in snapshot.verified:
            entry = snapshot.by_id[pair_id]
            self._maps[Venue.KALSHI].setdefault(entry.kalshi_ticker, []).append(
                (pair_id, entry.outcome_polarity)
            )
            self._maps[Venue.POLYMARKET].setdefault(entry.poly_market_slug, []).append(
                (pair_id, entry.outcome_polarity)
            )
        return list(self._maps[Venue.KALSHI]), list(self._maps[Venue.POLYMARKET])

    def _normalize_native(self, venue: Venue, native: NativeBook) -> None:
        for pair_id, polarity in self._maps[venue].get(native.native_id, ()):
            self._book_store.update(
                normalize(native, polarity=polarity, pair_id=pair_id)
            )

    def refresh_routes(self) -> None:
        """Cheap re-route on a registry change that did NOT change the market set
        (e.g. a polarity correction): rebuild the maps and RE-NORMALIZE every
        retained native book so the fix lands immediately, without tearing down
        the WS feeds."""
        self._build_maps()
        for venue, books in self._native.items():
            for native in books.values():
                self._normalize_native(venue, native)

    def plan(self) -> list[PlannedFeed]:
        kalshi_ids, poly_ids = self._build_maps()
        planned: list[PlannedFeed] = []
        planned += self._plan_venue(Venue.KALSHI, kalshi_ids)
        planned += self._plan_venue(Venue.POLYMARKET, poly_ids)
        self._feeds = [p.feed for p in planned]
        _log.info(
            "subscription_planned",
            kalshi=len(kalshi_ids),
            poly=len(poly_ids),
            feeds=len(planned),
        )
        return planned

    def _on_book(self, venue: Venue) -> Callable[[NativeBook], None]:
        def handler(native: NativeBook) -> None:
            self._native[venue][native.native_id] = native  # retain for re-route
            self._normalize_native(venue, native)

        return handler

    def _plan_venue(self, venue: Venue, ids: list[str]) -> list[PlannedFeed]:
        if not ids:
            return []
        on_book = self._on_book(venue)
        if venue is Venue.KALSHI:
            client = KalshiClient(self._settings, self._http)
            if self._settings.kalshi_credentials_present:
                assert self._settings.kalshi_private_key_path is not None
                signer = KalshiSigner(
                    self._settings.kalshi_api_key_id or "", self._settings.kalshi_private_key_path
                )
                shards = shard(ids, max_per_conn=1000, max_conns=self._settings.kalshi_max_ws_conns)
                return [
                    self._ws_feed(KalshiStream(self._settings, signer), s, on_book) for s in shards
                ]
            return [self._poll_feed(client, ids, on_book)]
        else:
            pclient = PolymarketClient(self._settings, self._http)
            if self._settings.poly_credentials_present:
                psigner = PolymarketSigner(
                    self._settings.poly_access_key or "", self._settings.poly_ed25519_seed or ""
                )
                shards = shard(
                    ids, max_per_conn=self._settings.poly_markets_per_conn, max_conns=10_000
                )
                return [
                    self._ws_feed(PolymarketStream(self._settings, psigner), s, on_book)
                    for s in shards
                ]
            return [self._poll_feed(pclient, ids, on_book)]

    def _poll_feed(self, client, ids, on_book) -> PlannedFeed:  # type: ignore[no-untyped-def]
        feed = RestPollFeed(client=client, interval_ms=self._settings.rest_poll_interval_ms)
        return PlannedFeed(feed=feed, native_ids=ids, runner=lambda: feed.run(ids, on_book))

    def _ws_feed(self, feed, ids, on_book) -> PlannedFeed:  # type: ignore[no-untyped-def]
        return PlannedFeed(feed=feed, native_ids=ids, runner=lambda: feed.run(ids, on_book))
