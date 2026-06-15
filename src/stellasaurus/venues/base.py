"""Venue-agnostic adapter interface + a generic REST-polling feed.

The rest of the system (catalog sync, normalization, ingestion) talks only to
these abstractions, never to a specific venue. Each venue supplies a
``VenueClient`` (REST catalog + book snapshots) and one or more ``MarketFeed``s
(live updates over WS, or REST-poll fallback when unauthenticated).

Adapters emit ``NativeBook`` (native YES/NO terms, micro-USD). Mapping to
canonical-YES happens later in ``hot_path.normalize`` — keeping this layer dumb.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from stellasaurus.common.clock import wall_ms
from stellasaurus.common.ids import terms_fingerprint
from stellasaurus.common.logging import get_logger
from stellasaurus.common.types import Venue
from stellasaurus.hot_path.book import NativeBook

_log = get_logger("venues")

# Called with each fresh native book. Must be cheap / non-blocking.
OnBook = Callable[[NativeBook], None]


@dataclass(frozen=True, slots=True)
class RawMarket:
    """A market as enumerated from a venue catalog (native terms)."""

    venue: Venue
    native_id: str
    title: str
    rules_text: str | None
    settlement_source: str | None
    resolves_at_ms: int | None
    status: str | None
    raw: dict[str, object]


def market_fingerprint(m: RawMarket) -> str:
    """Fingerprint over ONLY the fields that define the resolution terms.

    Excludes volatile fields (volume, price) so unrelated churn doesn't trip false
    STALEs, while any change to the proposition / source / timing does. Shared by
    catalog sync and the seed resolver so a market's fingerprint is computed one way.
    """
    return terms_fingerprint(
        {
            "title": m.title,
            "rules": m.rules_text,
            "settlement_source": m.settlement_source,
            "resolves_at_ms": m.resolves_at_ms,
        }
    )


@dataclass(slots=True)
class FeedStats:
    venue: Venue
    transport: str  # "WS" | "REST_POLL" | "NONE"
    connected: bool = False
    frames: int = 0
    reconnects: int = 0
    last_frame_ms: int | None = None

    def mark_frame(self) -> None:
        self.frames += 1
        self.last_frame_ms = wall_ms()


class VenueClient(Protocol):
    venue: Venue

    async def list_markets(self) -> list[RawMarket]: ...

    async def get_market(self, native_id: str) -> RawMarket | None: ...

    async def get_book(self, native_id: str) -> NativeBook | None: ...


class MarketFeed(Protocol):
    stats: FeedStats

    async def run(self, native_ids: Sequence[str], on_book: OnBook) -> None: ...


@dataclass
class RestPollFeed:
    """Fallback feed: poll ``VenueClient.get_book`` on an interval.

    Used when a venue's streaming transport requires auth we don't have in
    keyless Phase 1 (both Kalshi and Polymarket WS need an authenticated
    handshake). Produces identical ``NativeBook`` updates to the WS path, so the
    downstream pipeline is unchanged — just lower-frequency.
    """

    client: VenueClient
    interval_ms: int
    stats: FeedStats = field(init=False)

    def __post_init__(self) -> None:
        self.stats = FeedStats(venue=self.client.venue, transport="REST_POLL")

    async def run(self, native_ids: Sequence[str], on_book: OnBook) -> None:
        self.stats.connected = True
        interval = self.interval_ms / 1000.0
        try:
            while True:
                for native_id in native_ids:
                    try:
                        book = await self.client.get_book(native_id)
                    except Exception as exc:  # noqa: BLE001 - feed must not die on one book
                        _log.warning("poll_book_failed", venue=self.stats.venue.value,
                                     native_id=native_id, error=str(exc))
                        continue
                    if book is not None:
                        self.stats.mark_frame()
                        on_book(book)
                await asyncio.sleep(interval)
        finally:
            self.stats.connected = False
