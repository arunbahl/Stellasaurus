"""Latency: recorder correctness + an evaluator micro-benchmark with a
regression threshold (DESIGN §11: 'regression thresholds on the eval stage')."""

import time
from decimal import Decimal

from stellasaurus.common.types import OutcomePolarity, PairSource, PairStatus, Venue
from stellasaurus.hot_path.book import NativeBook, PriceLevel
from stellasaurus.hot_path.evaluator import OpportunityEvaluator
from stellasaurus.hot_path.fees import FeeParams
from stellasaurus.hot_path.latency import LatencyRecorder
from stellasaurus.hot_path.normalize import normalize
from stellasaurus.hot_path.opportunities import OpportunitySink
from stellasaurus.hot_path.snapshot import (
    LimitsSnapshot,
    PairRegistryEntry,
    RegistrySnapshot,
)
from stellasaurus.hot_path.state import HotStateStore

PARAMS = FeeParams(
    kalshi_taker_multiplier=Decimal("0.07"),
    kalshi_maker_multiplier=Decimal("0.0175"),
    kalshi_precision_micros=10_000,
    poly_taker_coefficient=Decimal("0.06"),
    poly_maker_coefficient=Decimal("-0.0125"),
)


def test_latency_recorder_stats():
    rec = LatencyRecorder(size=8)
    for us in (100, 200, 300, 400):
        rec.record("eval", us * 1000)
    (s,) = rec.snapshot()
    assert s.stage == "eval" and s.count == 4
    assert s.avg_us == 250 and s.max_us == 400


def test_evaluator_latency_under_regression_threshold():
    now_ms = 1_000_000_000_000
    entry = PairRegistryEntry(
        "p1", "prop", "KX-A", "slug", OutcomePolarity.DIRECT, PairStatus.VERIFIED,
        now_ms + 10 * 86_400_000, None, 0, "fp", PairSource.LLM,
    )
    store = HotStateStore(
        registry=RegistrySnapshot.build(1, [entry], now_ms=now_ms),
        limits=LimitsSnapshot(1, False, 20_000, 0.10, 10, 50_000_000, 500_000_000,
                              10**12, 100, 10**12, 0.5),
        book_staleness_ms=60_000,
    )
    # 10-level ladders on both venues
    levels = tuple(PriceLevel(400_000 + i * 10_000, 50) for i in range(10))
    bids = tuple(PriceLevel(390_000 - i * 10_000, 50) for i in range(10))
    for venue in (Venue.KALSHI, Venue.POLYMARKET):
        nb = NativeBook(venue, "x", bids, levels, None, None, 1,
                        time.monotonic_ns(), now_ms)
        store.publish_book(normalize(nb, polarity=OutcomePolarity.DIRECT, pair_id="p1"))

    ev = OpportunityEvaluator(state=store, fee_params=PARAMS, sink=OpportunitySink())
    # warmup
    for _ in range(50):
        ev.on_book_update("p1")
    n = 500
    start = time.perf_counter_ns()
    for _ in range(n):
        ev.on_book_update("p1")
    per_call_us = (time.perf_counter_ns() - start) / n / 1000
    # §8: the eval stage is the part we fully control. Python target: < 500µs
    # per update (network legs are ~milliseconds); regression gate at 2ms.
    assert per_call_us < 2_000, f"evaluator too slow: {per_call_us:.0f}µs/update"
