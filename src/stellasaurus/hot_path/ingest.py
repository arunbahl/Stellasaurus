"""BookStore — the write-side facade for normalized market data.

Venue streams push ``NormalizedBook`` updates here. Each update atomically
replaces the per-(pair, venue) book ref and notifies registered listeners with
the affected ``pair_id`` — the event-driven hook the Phase-3 evaluator attaches
to (DESIGN §6.6: "Trigger: any book update for a VERIFIED pair").

GO-REWRITABLE BOUNDARY: stdlib + ``common`` + sibling hot_path modules only.
"""

from __future__ import annotations

from collections.abc import Callable

from stellasaurus.hot_path.book import NormalizedBook
from stellasaurus.hot_path.state import HotStateStore

BookListener = Callable[[str], None]


class BookStore:
    def __init__(self, store: HotStateStore) -> None:
        self._store = store
        self._listeners: list[BookListener] = []

    def add_listener(self, listener: BookListener) -> None:
        """Register an on-book-update callback. Phase 1 has none; Phase 3 adds
        the evaluator. Listeners must be cheap and non-blocking (hot path)."""
        self._listeners.append(listener)

    def update(self, book: NormalizedBook) -> None:
        self._store.publish_book(book)
        for listener in self._listeners:
            listener(book.pair_id)
