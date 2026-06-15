"""Offline replay harness: feed recorded venue frames through
parse -> normalize -> BookStore and assert the hot state reflects the latest.

Deterministic, no network. This is the substrate the Phase-3 evaluator replay
(DESIGN §11) will reuse.
"""

import json
from pathlib import Path

from stellasaurus.common.clock import mono_ns
from stellasaurus.common.types import OutcomePolarity, PairSource, PairStatus, Venue
from stellasaurus.hot_path.ingest import BookStore
from stellasaurus.hot_path.normalize import normalize
from stellasaurus.hot_path.snapshot import (
    LimitsSnapshot,
    PairRegistryEntry,
    RegistrySnapshot,
)
from stellasaurus.hot_path.state import HotStateStore
from stellasaurus.venues.kalshi import parse as kparse
from stellasaurus.venues.polymarket import parse as pparse

FIXTURE = Path(__file__).parent / "fixtures" / "btc_pair.jsonl"
PAIR_ID = "btc"


def _store() -> HotStateStore:
    entry = PairRegistryEntry(
        PAIR_ID, "BTC 100k", "KXBTC", "btc-100k", OutcomePolarity.DIRECT,
        PairStatus.VERIFIED, None, None, 0, "fp", PairSource.MANUAL_SEED,
    )
    return HotStateStore(
        registry=RegistrySnapshot.build(1, [entry]),
        limits=LimitsSnapshot(1, True, 0, 0.0, 1, 1, 1, 1, 1, 1, 0.5),
        book_staleness_ms=10_000,
    )


def _native(venue: str, native_id: str, payload: dict, seq: int):
    # Stamp with real monotonic time so the staleness check (vs now()) is meaningful.
    now = mono_ns()
    if venue == "KALSHI":
        return kparse.parse_orderbook(
            ticker=native_id, payload=payload, seq=seq, recv_mono_ns=now, recv_wall_ms=seq
        )
    return pparse.parse_book(
        slug=native_id, payload=payload, seq=seq, recv_mono_ns=now, recv_wall_ms=seq
    )


def test_replay_populates_normalized_books_and_listener():
    store = _store()
    book_store = BookStore(store)
    fired: list[str] = []
    book_store.add_listener(fired.append)

    for seq, line in enumerate(FIXTURE.read_text().splitlines(), start=1):
        frame = json.loads(line)
        native = _native(frame["venue"], frame["native_id"], frame["payload"], seq)
        book = normalize(native, polarity=OutcomePolarity.DIRECT, pair_id=PAIR_ID)
        book_store.update(book)

    # Listener fired once per frame, always with the pair id.
    assert fired == [PAIR_ID, PAIR_ID, PAIR_ID]

    # Latest Kalshi frame wins: yes bid 0.59 -> NO ask derived 0.41.
    kalshi = store.book(PAIR_ID, Venue.KALSHI)
    assert kalshi is not None
    assert kalshi.best_yes_bid.price == 590_000
    assert kalshi.best_no_ask.price == 410_000  # 1 - 0.59

    # Polymarket YES ask is native at 0.62.
    poly = store.book(PAIR_ID, Venue.POLYMARKET)
    assert poly is not None
    assert poly.best_yes_ask.price == 620_000

    # Both legs present and within staleness -> evaluable.
    assert store.is_fresh(PAIR_ID) is True
