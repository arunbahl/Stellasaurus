"""Phase 4: risk gate, paper execution (FOK + unwind), halt controller."""

from decimal import Decimal

from stellasaurus.background.halt import HaltController
from stellasaurus.common.types import OutcomePolarity, PairSource, PairStatus, Venue
from stellasaurus.hot_path.book import NativeBook, PriceLevel
from stellasaurus.hot_path.evaluator import OpportunityEvaluator
from stellasaurus.hot_path.execution import PaperExecutionEngine
from stellasaurus.hot_path.fees import FeeParams
from stellasaurus.hot_path.normalize import normalize
from stellasaurus.hot_path.opportunities import OpportunitySink
from stellasaurus.hot_path.positions import HedgeStatus, PositionsStore
from stellasaurus.hot_path.risk import RiskManager
from stellasaurus.hot_path.seams import TradeIntent
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
    poly_taker_coefficient=Decimal("0.06"), poly_maker_coefficient=Decimal("-0.0125"),
)


class FakeClock:
    def __init__(self, t_ms: int = 1_000_000_000_000) -> None:
        self.t_ms = t_ms

    def mono_ns(self) -> int:
        return self.t_ms * 1_000_000

    def wall_ms(self) -> int:
        return self.t_ms


def _limits(**kw) -> LimitsSnapshot:
    base = dict(
        version=1, halted=False, theta_micros=20_000, hurdle=0.10,
        target_size_default=10, max_bet_value_micros=50_000_000,
        max_bet_value_ceiling_micros=500_000_000,
        max_aggregate_exposure_micros=10**12, max_open_pairs=100,
        max_committed_capital_micros=10**12, min_t_days=0.5,
    )
    base.update(kw)
    return LimitsSnapshot(**base)


def _store(clock, limits=None, pair_id="p1"):
    entry = PairRegistryEntry(
        pair_id, "prop", "KX", "slug", OutcomePolarity.DIRECT, PairStatus.VERIFIED,
        clock.t_ms + 10 * 86_400_000, None, 0, "fp", PairSource.LLM,
    )
    return HotStateStore(
        registry=RegistrySnapshot.build(1, [entry], now_ms=clock.t_ms),
        limits=limits or _limits(),
        book_staleness_ms=5_000,
        clock=clock,
    )


def _push_books(store, clock, *, yes_ask_k, yes_bid_k, yes_ask_p, yes_bid_p, sizes=1000):
    for venue, bid, ask in (
        (Venue.KALSHI, yes_bid_k, yes_ask_k),
        (Venue.POLYMARKET, yes_bid_p, yes_ask_p),
    ):
        nb = NativeBook(
            venue, "x", (PriceLevel(bid, sizes),), (PriceLevel(ask, sizes),),
            None, None, 1, clock.mono_ns(), clock.t_ms,
        )
        store.publish_book(normalize(nb, polarity=OutcomePolarity.DIRECT, pair_id="p1"))


def _intent(qty=10, vy=400_000, vn=450_000) -> TradeIntent:
    return TradeIntent(
        pair_id="p1", orientation="A", qty=qty,
        yes_venue=Venue.KALSHI, no_venue=Venue.POLYMARKET,
        vwap_yes_micros=vy, vwap_no_micros=vn,
        net_edge_micros=100_000, created_mono_ns=0,
    )


def _fresh_setup(limits=None):
    clock = FakeClock()
    store = _store(clock, limits)
    _push_books(store, clock, yes_ask_k=400_000, yes_bid_k=380_000,
                yes_ask_p=570_000, yes_bid_p=550_000)
    positions = PositionsStore()
    risk = RiskManager(state=store, positions=positions, clock=clock)
    return clock, store, positions, risk


# --- risk gate ---

def test_risk_approves_clean_intent():
    _, _, _, risk = _fresh_setup()
    assert risk.approve(_intent()) is True
    d = risk.decisions()[-1]
    assert d.approved and d.rejected_by is None


def test_risk_rejects_when_halted():
    _, _, _, risk = _fresh_setup(_limits(halted=True))
    assert risk.approve(_intent()) is False
    assert risk.decisions()[-1].rejected_by == "halted"


def test_risk_rejects_duplicate_open_pair():
    clock, store, positions, risk = _fresh_setup()
    executor = PaperExecutionEngine(
        state=store, positions=positions, fee_params=PARAMS,
        slippage_tolerance_bips=50, clock=clock,
    )
    executor.submit(_intent())
    assert positions.has_open("p1")
    assert risk.approve(_intent()) is False
    assert risk.decisions()[-1].rejected_by == "pair_already_open"


def test_risk_rejects_over_max_bet_value():
    _, _, _, risk = _fresh_setup(_limits(max_bet_value_micros=1_000_000))
    # 10 * (0.40+0.45) = $8.50 committed > $1 cap
    assert risk.approve(_intent()) is False
    assert risk.decisions()[-1].rejected_by == "max_bet_value"


def test_risk_rejects_over_committed_capital():
    _, _, _, risk = _fresh_setup(_limits(max_committed_capital_micros=1_000_000))
    assert risk.approve(_intent()) is False
    assert risk.decisions()[-1].rejected_by == "max_committed_capital"


# --- paper executor ---

def test_executor_fills_both_legs_hedged():
    clock, store, positions, _ = _fresh_setup()
    ex = PaperExecutionEngine(state=store, positions=positions, fee_params=PARAMS,
                              slippage_tolerance_bips=50, clock=clock)
    ex.submit(_intent())
    (p,) = positions.open_positions()
    assert p.hedge_status is HedgeStatus.HEDGED
    assert p.yes_price_micros == 400_000 and p.no_price_micros == 450_000
    assert p.committed_micros == 10 * 850_000 + p.fees_micros
    t = positions.totals()
    assert t.open_pairs == 1 and t.committed_micros == p.committed_micros


def test_executor_unwinds_single_leg_when_no_side_moved():
    clock = FakeClock()
    store = _store(clock)
    # NO leg (Poly, derived from yes bids) now costs 0.52 (> intent 0.45 + 50bips)
    _push_books(store, clock, yes_ask_k=400_000, yes_bid_k=380_000,
                yes_ask_p=570_000, yes_bid_p=480_000)
    positions = PositionsStore()
    ex = PaperExecutionEngine(state=store, positions=positions, fee_params=PARAMS,
                              slippage_tolerance_bips=50, clock=clock)
    ex.submit(_intent())
    (p,) = positions.open_positions()
    assert p.hedge_status is HedgeStatus.UNWOUND
    assert p.committed_micros == 0
    # bought YES at 0.40, sold back at the 0.38 bid -> 10 * 0.02 + fees
    assert p.unwind_loss_micros is not None and p.unwind_loss_micros >= 200_000
    assert positions.totals().unwind_count == 1
    assert not positions.has_open("p1")


def test_executor_fails_flat_when_both_legs_moved():
    clock = FakeClock()
    store = _store(clock)
    _push_books(store, clock, yes_ask_k=500_000, yes_bid_k=480_000,
                yes_ask_p=570_000, yes_bid_p=480_000)  # both beyond tolerance
    positions = PositionsStore()
    ex = PaperExecutionEngine(state=store, positions=positions, fee_params=PARAMS,
                              slippage_tolerance_bips=50, clock=clock)
    ex.submit(_intent())
    (p,) = positions.open_positions()
    assert p.hedge_status is HedgeStatus.FAILED
    assert p.committed_micros == 0 and p.unwind_loss_micros is None


# --- evaluator -> risk -> executor integration ---

def test_fire_opens_position_and_second_fire_deduped():
    clock, store, positions, risk = _fresh_setup()
    ex = PaperExecutionEngine(state=store, positions=positions, fee_params=PARAMS,
                              slippage_tolerance_bips=50, clock=clock)
    sink = OpportunitySink()
    ev = OpportunityEvaluator(state=store, fee_params=PARAMS, sink=sink,
                              risk_gate=risk, executor=ex, clock=clock)
    ev.on_book_update("p1")
    assert positions.totals().open_pairs == 1
    ev.on_book_update("p1")  # same dislocation again
    assert positions.totals().open_pairs == 1  # deduped by risk gate
    assert risk.decisions()[-1].rejected_by == "pair_already_open"


# --- halt controller ---

class _NullAudit:
    def append(self, **kw) -> None:  # noqa: ANN003
        pass


def test_set_halted_publishes_snapshot():
    clock, store, positions, _ = _fresh_setup()
    hc = HaltController(store=store, positions=positions, audit_repo=_NullAudit())
    hc.set_halted(True, actor="test", reason="unit")
    assert store.limits().halted is True
    hc.set_halted(False, actor="test", reason="unit")
    assert store.limits().halted is False


def test_update_limits_validates_and_clamps():
    clock, store, positions, _ = _fresh_setup()
    hc = HaltController(store=store, positions=positions, audit_repo=_NullAudit())
    errors = hc.update_limits(
        {"theta_micros": 30_000, "max_bet_value_micros": 10**12,
         "hurdle": "not-a-number", "unknown_field": 1, "max_open_pairs": -5},
        actor="test",
    )
    limits = store.limits()
    assert limits.theta_micros == 30_000
    # clamped to the non-UI ceiling
    assert limits.max_bet_value_micros == limits.max_bet_value_ceiling_micros
    assert set(errors) == {"hurdle", "unknown_field", "max_open_pairs"}
    assert limits.hurdle == 0.10  # invalid value -> prior retained


async def test_auto_halt_on_all_pairs_stale():
    clock = FakeClock()
    store = _store(clock)
    _push_books(store, clock, yes_ask_k=400_000, yes_bid_k=380_000,
                yes_ask_p=570_000, yes_bid_p=550_000)
    positions = PositionsStore()
    hc = HaltController(store=store, positions=positions, audit_repo=_NullAudit(),
                        auto_halt_stale_seconds=30, clock=clock)
    await hc.watch_once()
    assert store.limits().halted is False
    clock.t_ms += 120_000  # books now stale
    await hc.watch_once()  # starts the stale timer
    assert store.limits().halted is False
    clock.t_ms += 60_000  # stale for > threshold
    await hc.watch_once()
    assert store.limits().halted is True