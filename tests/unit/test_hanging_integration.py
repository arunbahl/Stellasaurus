"""End-to-end HANGING chain with the REAL control-plane components.

Unlike test_live_engine_hanging_leg_halts_and_enqueues (which fakes the halt
callback and flattener), this wires the actual HaltController, PositionFlattener
worker, and HotStateStore exactly as app.py does, and proves:

  filled leg + failing unwind
    -> engine records HANGING
    -> HaltController flips store.limits().halted True (control-plane propagation)
    -> flattener worker dequeues the naked leg and closes it to flat

Gateways are scripted (no network); the real-money primitives (fill detection
under lag, close_position) were validated live separately.
"""

import asyncio

from stellasaurus.background.flattener import PositionFlattener
from stellasaurus.background.halt import HaltController
from stellasaurus.background.live_execution import LiveExecutionEngine
from stellasaurus.common.types import (
    OutcomePolarity,
    PairSource,
    PairStatus,
    Venue,
)
from stellasaurus.hot_path.positions import HedgeStatus, PositionsStore
from stellasaurus.hot_path.seams import TradeIntent
from stellasaurus.hot_path.snapshot import (
    LimitsSnapshot,
    PairRegistryEntry,
    RegistrySnapshot,
)
from stellasaurus.hot_path.state import HotStateStore
from stellasaurus.storage.audit_repo import AuditRepo
from stellasaurus.storage.db import Database
from stellasaurus.venues.orders import OrderResult


class ScriptedGateway:
    """Entry leg fills; every unwind buy_fok returns zero-fill (no raise, like
    Kalshi's 409). close_position reports the position flattened."""

    def __init__(self, venue: Venue, fill_entry: bool):
        self.venue = venue
        self._fill_entry = fill_entry
        self._calls = 0
        self.closed = 0

    async def buy_fok(self, *, native_id, side, qty, limit_price_micros, polarity):
        self._calls += 1
        filled = qty if (self._calls == 1 and self._fill_entry) else 0
        return OrderResult(self.venue, native_id, side, qty, filled,
                           500_000 if filled else None, 0, "oid", {})

    async def net_position(self, native_id):
        return 1 if (self._fill_entry and self.closed == 0) else 0

    async def close_position(self, native_id):
        self.closed += 1
        return 0  # flattened


async def test_hanging_chain_end_to_end(tmp_path):
    db = Database(tmp_path / "t.db")
    db.migrate()
    positions = PositionsStore()
    entry = PairRegistryEntry("p", "P", "KX", "slug", OutcomePolarity.DIRECT,
                              PairStatus.VERIFIED, 10**13, None, 0, "fp",
                              PairSource.STRUCTURED)
    store = HotStateStore(
        registry=RegistrySnapshot.build(1, [entry], now_ms=0),
        limits=LimitsSnapshot(1, False, 0, 0.0, 1, 10**8, 10**9, 10**12, 10,
                              10**12, 0.5),
        book_staleness_ms=60_000,
    )
    # REAL control-plane components, wired as in app.py
    halt = HaltController(store=store, positions=positions, audit_repo=AuditRepo(db))
    kalshi_gw = ScriptedGateway(Venue.KALSHI, fill_entry=True)
    poly_gw = ScriptedGateway(Venue.POLYMARKET, fill_entry=False)
    flattener = PositionFlattener(
        gateways={Venue.KALSHI: kalshi_gw, Venue.POLYMARKET: poly_gw},
        max_attempts=3, backoff_seconds=0,
    )
    engine = LiveExecutionEngine(
        state=store, positions=positions,
        gateways={Venue.KALSHI: kalshi_gw, Venue.POLYMARKET: poly_gw},
        slippage_tolerance_bips=50,
        halt=lambda reason: halt.set_halted(True, actor="live_execution", reason=reason),
        flattener=flattener,
    )

    assert store.limits().halted is False
    intent = TradeIntent(pair_id="p", orientation="A", qty=1,
                         yes_venue=Venue.KALSHI, no_venue=Venue.POLYMARKET,
                         vwap_yes_micros=500_000, vwap_no_micros=490_000,
                         net_edge_micros=10_000, created_mono_ns=0)
    await engine._execute(intent)

    # 1. engine recorded HANGING
    recorded = positions.open_positions()
    assert len(recorded) == 1 and recorded[0].hedge_status is HedgeStatus.HANGING
    # 2. REAL HaltController flipped the hot-path store's halted flag
    assert store.limits().halted is True
    # 3. flattener worker dequeues the naked leg and closes it
    worker = asyncio.create_task(flattener.run())
    for _ in range(50):
        await asyncio.sleep(0)
        if kalshi_gw.closed:
            break
    await asyncio.sleep(0)
    worker.cancel()
    assert kalshi_gw.closed >= 1  # the filled (Kalshi) leg was auto-flattened
