"""OpportunityEvaluator: gate ladder, both orientations, sizing, paper fires."""

from decimal import Decimal

from stellasaurus.common.money import PAYOUT_MICROS
from stellasaurus.common.types import OutcomePolarity, PairSource, PairStatus, Venue
from stellasaurus.hot_path.book import NativeBook, PriceLevel
from stellasaurus.hot_path.evaluator import OpportunityEvaluator
from stellasaurus.hot_path.fees import FeeParams
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
    poly_taker_bps=10,
    poly_maker_bps=0,
    poly_min_fee_micros=1_000,
)


class FakeClock:
    def __init__(self, t_ms: int = 1_000_000_000_000) -> None:
        self.t_ms = t_ms

    def mono_ns(self) -> int:
        return self.t_ms * 1_000_000

    def wall_ms(self) -> int:
        return self.t_ms


def _limits(theta=20_000, hurdle=0.10, target=10, max_bet=50_000_000) -> LimitsSnapshot:
    return LimitsSnapshot(
        version=1, halted=False, theta_micros=theta, hurdle=hurdle,
        target_size_default=target, max_bet_value_micros=max_bet,
        max_bet_value_ceiling_micros=max_bet * 10,
        max_aggregate_exposure_micros=10**12, max_open_pairs=100,
        max_committed_capital_micros=10**12, min_t_days=0.5,
    )


def _setup(*, yes_ask_k, yes_bid_k, yes_ask_p, yes_bid_p, sizes=1000,
           limits=None, resolves_days=10.0):
    clock = FakeClock()
    entry = PairRegistryEntry(
        "p1", "prop", "KX", "slug", OutcomePolarity.DIRECT, PairStatus.VERIFIED,
        int(clock.t_ms + resolves_days * 86_400_000), None, 0, "fp", PairSource.LLM,
    )
    store = HotStateStore(
        registry=RegistrySnapshot.build(1, [entry], now_ms=clock.t_ms),
        limits=limits or _limits(),
        book_staleness_ms=5_000,
        clock=clock,
    )
    for venue, bid, ask in (
        (Venue.KALSHI, yes_bid_k, yes_ask_k),
        (Venue.POLYMARKET, yes_bid_p, yes_ask_p),
    ):
        nb = NativeBook(
            venue, "x",
            yes_bids=(PriceLevel(bid, sizes),),
            yes_asks=(PriceLevel(ask, sizes),),
            no_bids=None, no_asks=None,
            seq=1, recv_mono_ns=clock.mono_ns(), recv_wall_ms=clock.t_ms,
        )
        store.publish_book(normalize(nb, polarity=OutcomePolarity.DIRECT, pair_id="p1"))
    sink = OpportunitySink()
    ev = OpportunityEvaluator(state=store, fee_params=PARAMS, sink=sink, clock=clock)
    return ev, sink, store


def _by_orientation(sink):
    return {o.orientation: o for o in sink.latest()}


def test_fires_on_clear_dislocation():
    # Kalshi YES ask $0.40; Poly YES bid $0.55 -> Poly NO ask $0.45 (derived).
    # Orientation A: 0.40 + 0.45 = 0.85 -> big net edge, fires.
    ev, sink, _ = _setup(
        yes_ask_k=400_000, yes_bid_k=380_000,
        yes_ask_p=570_000, yes_bid_p=550_000,
    )
    ev.on_book_update("p1")
    opps = _by_orientation(sink)
    a = opps["A"]
    assert a.would_fire and a.gate_failed is None
    assert a.qty == 10
    assert a.vwap_yes_micros == 400_000 and a.vwap_no_micros == 450_000
    # fees: kalshi 10@0.40 -> 0.07*10*0.4*0.6=$0.168->ceil $0.17; poly 10@0.45
    # notional $4.50 -> $0.0045; per pair = ceil((170000+4500)/10) = 17450
    assert a.fees_per_pair_micros == 17_450
    assert a.net_edge_micros == PAYOUT_MICROS - (400_000 + 450_000 + 17_450)
    assert a.annualized_return is not None and a.annualized_return > 1.0
    # Orientation B costs 0.57 + 0.62 -> way negative, blocked at theta.
    assert opps["B"].would_fire is False and opps["B"].gate_failed == "theta"
    assert sink.fired() == (a,)


def test_theta_blocks_marginal_edge():
    # Combined cost ~0.985 + fees -> tiny positive edge below theta ($0.02).
    ev, sink, _ = _setup(
        yes_ask_k=500_000, yes_bid_k=480_000,
        yes_ask_p=530_000, yes_bid_p=515_000,  # derived NO ask = 0.485
    )
    ev.on_book_update("p1")
    a = _by_orientation(sink)["A"]
    assert not a.would_fire and a.gate_failed == "theta"
    assert a.net_edge_micros is not None and a.net_edge_micros < 20_000


def test_hurdle_blocks_slow_resolution():
    # Same clear dislocation as the firing test but resolving in 300 days:
    # ann = (net/committed) * 365/300 must stay below a punishing hurdle.
    ev, sink, _ = _setup(
        yes_ask_k=400_000, yes_bid_k=380_000,
        yes_ask_p=570_000, yes_bid_p=550_000,
        limits=_limits(hurdle=100.0), resolves_days=300.0,
    )
    ev.on_book_update("p1")
    a = _by_orientation(sink)["A"]
    assert not a.would_fire and a.gate_failed == "hurdle"
    assert a.annualized_return is not None


def test_stale_book_blocks_everything():
    ev, sink, store = _setup(
        yes_ask_k=400_000, yes_bid_k=380_000,
        yes_ask_p=570_000, yes_bid_p=550_000,
    )
    store._clock.t_ms += 60_000  # books now 60s old vs 5s staleness
    ev.on_book_update("p1")
    for o in sink.latest():
        assert not o.would_fire and o.gate_failed == "stale_book"


def test_depth_caps_qty():
    # Only 3 contracts on each side -> Q reduced to 3, still fires.
    ev, sink, _ = _setup(
        yes_ask_k=400_000, yes_bid_k=380_000,
        yes_ask_p=570_000, yes_bid_p=550_000,
        sizes=3,
    )
    ev.on_book_update("p1")
    a = _by_orientation(sink)["A"]
    assert a.would_fire and a.qty == 3


def test_max_bet_value_caps_qty():
    # max_bet_value $2 -> at ~$0.85/pair only 2 pairs fit.
    ev, sink, _ = _setup(
        yes_ask_k=400_000, yes_bid_k=380_000,
        yes_ask_p=570_000, yes_bid_p=550_000,
        limits=_limits(max_bet=2_000_000),
    )
    ev.on_book_update("p1")
    a = _by_orientation(sink)["A"]
    assert a.would_fire and a.qty == 2


def test_unverified_pair_ignored():
    ev, sink, store = _setup(
        yes_ask_k=400_000, yes_bid_k=380_000,
        yes_ask_p=570_000, yes_bid_p=550_000,
    )
    store.publish_registry(RegistrySnapshot.build(2, [], now_ms=0))
    ev.on_book_update("p1")
    assert sink.latest() == ()
