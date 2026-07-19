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

async def test_live_engine_hanging_leg_halts_and_enqueues(tmp_path):
    """One leg fills, the unwind can NEVER fill -> HANGING + halt + enqueue."""
    from stellasaurus.background.flattener import NakedLeg
    from stellasaurus.background.live_execution import LiveExecutionEngine
    from stellasaurus.common.types import OutcomePolarity, PairSource, PairStatus
    from stellasaurus.hot_path.positions import PositionsStore
    from stellasaurus.hot_path.seams import TradeIntent
    from stellasaurus.hot_path.snapshot import (
        LimitsSnapshot,
        PairRegistryEntry,
        RegistrySnapshot,
    )
    from stellasaurus.hot_path.state import HotStateStore
    from stellasaurus.venues.orders import OrderResult

    class ScriptedGateway:
        def __init__(self, venue, fill_first):
            self.venue = venue
            self._fill_first = fill_first
            self._n = 0

        async def buy_fok(self, *, native_id, side, qty, limit_price_micros, polarity):
            self._n += 1
            # first call (entry) fills iff fill_first; all unwind calls miss
            filled = qty if (self._n == 1 and self._fill_first) else 0
            return OrderResult(self.venue, native_id, side, qty, filled,
                               500_000 if filled else None, 0, "oid", {})

    entry = PairRegistryEntry("p", "P", "KX", "slug", OutcomePolarity.DIRECT,
                              PairStatus.VERIFIED, 10**13, None, 0, "fp", PairSource.STRUCTURED)
    store = HotStateStore(
        registry=RegistrySnapshot.build(1, [entry], now_ms=0),
        limits=LimitsSnapshot(1, False, 0, 0.0, 1, 10**8, 10**9, 10**12, 10, 10**12, 0.5),
        book_staleness_ms=60_000,
    )
    halts, enq = [], []
    positions = PositionsStore()
    engine = LiveExecutionEngine(
        state=store, positions=positions,
        gateways={Venue.KALSHI: ScriptedGateway(Venue.KALSHI, fill_first=True),
                  Venue.POLYMARKET: ScriptedGateway(Venue.POLYMARKET, fill_first=False)},
        slippage_tolerance_bips=50,
        halt=lambda reason: halts.append(reason),
        flattener=type("F", (), {"enqueue": lambda self, leg: enq.append(leg)})(),
    )
    intent = TradeIntent(pair_id="p", orientation="A", qty=1,
                         yes_venue=Venue.KALSHI, no_venue=Venue.POLYMARKET,
                         vwap_yes_micros=500_000, vwap_no_micros=490_000,
                         net_edge_micros=10_000, created_mono_ns=0)
    await engine._execute(intent)
    assert halts == ["hanging_leg"]
    assert len(enq) == 1 and isinstance(enq[0], NakedLeg)
    assert enq[0].venue is Venue.KALSHI  # the leg that actually filled
    recorded = positions.open_positions()
    assert len(recorded) == 1 and recorded[0].hedge_status is HedgeStatus.HANGING


def _risk_fixture(tmp_path, max_open_pairs=1, max_committed=10**12):
    from stellasaurus.common.types import OutcomePolarity, PairSource, PairStatus
    from stellasaurus.hot_path.positions import PositionsStore
    from stellasaurus.hot_path.risk import RiskManager
    from stellasaurus.hot_path.snapshot import (
        LimitsSnapshot,
        PairRegistryEntry,
        RegistrySnapshot,
    )
    from stellasaurus.hot_path.state import HotStateStore
    e1 = PairRegistryEntry("p1", "P1", "K1", "s1", OutcomePolarity.DIRECT,
                           PairStatus.VERIFIED, 10**13, None, 0, "fp", PairSource.STRUCTURED)
    e2 = PairRegistryEntry("p2", "P2", "K2", "s2", OutcomePolarity.DIRECT,
                           PairStatus.VERIFIED, 10**13, None, 0, "fp", PairSource.STRUCTURED)
    store = HotStateStore(
        registry=RegistrySnapshot.build(1, [e1, e2], now_ms=0),
        limits=LimitsSnapshot(1, False, 0, 0.0, 1, 10**9, 10**12, max_committed,
                              max_open_pairs, max_committed, 0.5),
        book_staleness_ms=10**9,
    )
    # make both pairs "fresh" by publishing books
    from stellasaurus.common.types import Venue
    from stellasaurus.hot_path.book import NativeBook, PriceLevel
    from stellasaurus.hot_path.normalize import normalize
    for pid, k, s in (("p1", "K1", "s1"), ("p2", "K2", "s2")):
        for v in (Venue.KALSHI, Venue.POLYMARKET):
            # recv_mono_ns must be CURRENT monotonic time: a 0 here silently
            # becomes stale once machine uptime exceeds the staleness threshold
            # (a real time-bomb that broke these tests at ~13 days uptime).
            import time as _time
            nb = NativeBook(v, k if v is Venue.KALSHI else s,
                            (PriceLevel(500_000, 100),), (PriceLevel(510_000, 100),),
                            None, None, 1, _time.monotonic_ns(), 0)
            store.publish_book(normalize(nb, polarity=OutcomePolarity.DIRECT, pair_id=pid))
    return RiskManager(state=store, positions=PositionsStore()), store


def test_reservations_stop_inflight_flood_on_same_pair(tmp_path):
    """The live bug: async execution means positions lag; without reservations
    N intents on ONE pair all clear the gate before any records."""
    from stellasaurus.common.types import Venue
    from stellasaurus.hot_path.seams import TradeIntent
    risk, _ = _risk_fixture(tmp_path, max_open_pairs=1)
    intent = TradeIntent("p1", "A", 1, Venue.KALSHI, Venue.POLYMARKET,
                         480_000, 500_000, 20_000, 0)
    assert risk.approve(intent) is True        # first reserves the slot
    assert risk.approve(intent) is False       # same pair now pair_already_open
    assert risk.approve(intent) is False       # still blocked (no positions yet)
    risk.release("p1")
    assert risk.approve(intent) is True         # slot freed -> allowed again


def test_reservations_enforce_max_open_pairs_across_pairs(tmp_path):
    from stellasaurus.common.types import Venue
    from stellasaurus.hot_path.seams import TradeIntent
    risk, _ = _risk_fixture(tmp_path, max_open_pairs=1)
    i1 = TradeIntent("p1", "A", 1, Venue.KALSHI, Venue.POLYMARKET, 480_000, 500_000, 20_000, 0)
    i2 = TradeIntent("p2", "A", 1, Venue.KALSHI, Venue.POLYMARKET, 480_000, 500_000, 20_000, 0)
    assert risk.approve(i1) is True
    assert risk.approve(i2) is False   # different pair, but max_open_pairs=1 incl. reservation
    risk.release("p1")
    assert risk.approve(i2) is True


# --- Findings 2/3/4 hardening ---

class _MonoClock:
    def __init__(self): self.t = 1_000_000_000_000
    def mono_ns(self): return self.t * 1_000_000
    def wall_ms(self): return self.t


def test_cooldown_blocks_reentry_after_nonhedged(tmp_path):
    from stellasaurus.common.types import Venue
    from stellasaurus.hot_path.seams import TradeIntent
    risk, _ = _risk_fixture(tmp_path, max_open_pairs=5)
    clk = _MonoClock()
    risk._clock = clk  # deterministic time
    i = TradeIntent("p1", "A", 1, Venue.KALSHI, Venue.POLYMARKET, 480_000, 500_000, 20_000, 0)
    assert risk.approve(i) is True
    risk.release("p1")
    risk.cooldown("p1")                       # simulate an UNWOUND/FAILED outcome
    assert risk.approve(i) is False
    assert risk.decisions()[-1].rejected_by == "cooldown"
    clk.t += 31_000                            # past the 30s window
    assert risk.approve(i) is True


def test_ttl_purges_orphaned_reservation_but_not_held(tmp_path):
    from stellasaurus.common.types import Venue
    from stellasaurus.hot_path.seams import TradeIntent
    risk, _ = _risk_fixture(tmp_path, max_open_pairs=1)
    clk = _MonoClock()
    risk._clock = clk
    i1 = TradeIntent("p1", "A", 1, Venue.KALSHI, Venue.POLYMARKET, 480_000, 500_000, 20_000, 0)
    i2 = TradeIntent("p2", "A", 1, Venue.KALSHI, Venue.POLYMARKET, 480_000, 500_000, 20_000, 0)
    assert risk.approve(i1) is True            # reserves p1 (in-flight)
    assert risk.approve(i2) is False           # blocked by max_open_pairs=1
    clk.t += 31_000                            # p1 reservation now orphaned (TTL)
    assert risk.approve(i2) is True            # purge frees the slot
    # a HELD (HANGING) reservation must NOT be purged
    risk.release("p2")
    assert risk.approve(i1) is True
    risk.mark_held("p1")
    clk.t += 100_000
    assert risk.approve(i2) is False           # still blocked: held survives TTL
    assert risk.decisions()[-1].rejected_by == "max_open_pairs"


def test_reprice_updates_reserved_capital(tmp_path):
    from stellasaurus.common.types import Venue
    from stellasaurus.hot_path.seams import TradeIntent
    # cap allows exactly one ~$1 pair with fee headroom; reprice higher blocks a 2nd
    risk, _ = _risk_fixture(tmp_path, max_open_pairs=5, max_committed=2_100_000)
    i1 = TradeIntent("p1", "A", 1, Venue.KALSHI, Venue.POLYMARKET, 480_000, 500_000, 20_000, 0)
    i2 = TradeIntent("p2", "A", 1, Venue.KALSHI, Venue.POLYMARKET, 480_000, 500_000, 20_000, 0)
    assert risk.approve(i1) is True            # reserves ~0.98*1.05 = ~1.03
    risk.reprice("p1", 1_900_000)              # requote came back much pricier
    assert risk.approve(i2) is False           # now the pool is nearly exhausted
    assert risk.decisions()[-1].rejected_by == "max_committed_capital"
