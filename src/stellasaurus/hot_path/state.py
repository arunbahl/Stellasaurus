"""The hot-path read interface and its in-memory implementation.

GO-REWRITABLE BOUNDARY: stdlib + ``common`` + sibling hot_path modules only.

``HotState`` is the *only* surface future hot-path components (evaluator, risk
gate) are allowed to read. The dashboard reads it too. Writes go exclusively
through ``ingest.BookStore`` and the background plane's snapshot publishers.
"""

from __future__ import annotations

import threading
from typing import Protocol

from stellasaurus.common.clock import Clock, SystemClock
from stellasaurus.common.types import Venue
from stellasaurus.hot_path.book import NormalizedBook
from stellasaurus.hot_path.snapshot import (
    AtomicRef,
    FeedHealth,
    LimitsSnapshot,
    RegistrySnapshot,
)


class HotState(Protocol):
    def registry(self) -> RegistrySnapshot: ...
    def limits(self) -> LimitsSnapshot: ...
    def book(self, pair_id: str, venue: Venue) -> NormalizedBook | None: ...
    def is_fresh(self, pair_id: str) -> bool: ...
    def feed_health(self) -> FeedHealth: ...


class HotStateStore:
    """Concrete hot state. Lock-free reads of immutable snapshots/books.

    The only lock here guards mutation of the *set* of per-(pair, venue) book
    refs (adding a new ref). Reading or replacing the value inside an existing
    ``AtomicRef`` needs no lock.
    """

    def __init__(
        self,
        *,
        registry: RegistrySnapshot,
        limits: LimitsSnapshot,
        book_staleness_ms: int,
        clock: Clock | None = None,
        book_max_quiet_ms: int = 600_000,
    ) -> None:
        self._registry: AtomicRef[RegistrySnapshot] = AtomicRef(registry)
        self._limits: AtomicRef[LimitsSnapshot] = AtomicRef(limits)
        self._feed_health: AtomicRef[FeedHealth] = AtomicRef(FeedHealth.empty())
        self._books: dict[tuple[str, Venue], AtomicRef[NormalizedBook]] = {}
        self._books_lock = threading.Lock()
        self._staleness_ns = book_staleness_ms * 1_000_000
        # A book that has NOT updated in this long is stale even if its venue
        # feed is alive — a settled/resolved market whose book froze at final
        # prices must not be treated as a live quote (it manufactured a
        # post-match phantom edge against the other, still-live, venue).
        self._max_quiet_ns = book_max_quiet_ms * 1_000_000
        self._clock = clock or SystemClock()
        # Last frame received per venue across ALL markets. Delta feeds only
        # push on change, so an unchanged book is still trustworthy while its
        # venue's feed is demonstrably alive (§6.5 staleness is feed-level).
        self._venue_frame: dict[Venue, AtomicRef[int]] = {
            v: AtomicRef(0) for v in Venue
        }

    # --- HotState (reads) ---
    def registry(self) -> RegistrySnapshot:
        return self._registry.get()

    def limits(self) -> LimitsSnapshot:
        return self._limits.get()

    def feed_health(self) -> FeedHealth:
        return self._feed_health.get()

    def book(self, pair_id: str, venue: Venue) -> NormalizedBook | None:
        ref = self._books.get((pair_id, venue))
        return ref.get() if ref is not None else None

    def is_fresh(self, pair_id: str) -> bool:
        """Both legs present, and each leg's venue demonstrably alive.

        A leg is fresh if its book updated within ``book_staleness_ms`` OR its
        venue delivered ANY frame within that window (delta feeds only push on
        change — an unchanged book on a live feed is current, not stale). A
        missing book, or a venue gone quiet entirely, makes the pair
        non-evaluable: you cannot assert "locked" on a dead feed (§6.5).
        """
        now = self._clock.mono_ns()
        for venue in (Venue.KALSHI, Venue.POLYMARKET):
            book = self.book(pair_id, venue)
            if book is None:
                return False
            book_age = now - book.recv_mono_ns
            if book_age <= self._staleness_ns:
                continue  # book itself is fresh
            venue_fresh = now - self._venue_frame[venue].get() <= self._staleness_ns
            # A quiet-but-live feed keeps an unchanged book valid — but only up
            # to _max_quiet_ns; beyond that the book is presumed frozen/settled.
            if not (venue_fresh and book_age <= self._max_quiet_ns):
                return False
        return True

    def book_age_ms(self, pair_id: str, venue: Venue) -> int | None:
        book = self.book(pair_id, venue)
        if book is None:
            return None
        return (self._clock.mono_ns() - book.recv_mono_ns) // 1_000_000

    # --- writes (used by ingest + background publishers only) ---
    def publish_registry(self, snapshot: RegistrySnapshot) -> None:
        self._registry.publish(snapshot)

    def publish_limits(self, snapshot: LimitsSnapshot) -> None:
        self._limits.publish(snapshot)

    def publish_feed_health(self, health: FeedHealth) -> None:
        self._feed_health.publish(health)

    def publish_book(self, book: NormalizedBook) -> None:
        self._venue_frame[book.venue].publish(book.recv_mono_ns)
        key = (book.pair_id, book.venue)
        ref = self._books.get(key)
        if ref is None:
            with self._books_lock:
                ref = self._books.get(key)
                if ref is None:
                    self._books[key] = AtomicRef(book)
                    return
        ref.publish(book)

    def active_book_keys(self) -> tuple[tuple[str, Venue], ...]:
        return tuple(self._books.keys())
