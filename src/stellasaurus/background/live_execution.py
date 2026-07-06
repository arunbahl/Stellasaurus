"""Live Execution Engine (DESIGN §6.7) — real orders, HARD-GATED, Phase 6.

⚠️  Requires ``live_trading_enabled=true`` AND both venues' credentials AND the
order gateways' shapes validated against demo/sandbox first. The composition
root refuses to wire this engine otherwise.

The hot-path evaluator stays synchronous: its ``submit(intent)`` only enqueues.
A single worker task performs the one network round-trip the DESIGN allows on
the critical path — both legs submitted concurrently as FOK:

  * both fill            -> HEDGED position recorded (venue-reported fees)
  * exactly one fills    -> immediate forced unwind of the filled leg as a
                            marketable order; realized loss recorded
  * neither fills        -> FAILED record, nothing held

Every outcome is pushed into the same PositionsStore the paper engine uses, so
risk limits, dashboards, and P&L settlement treat live and paper uniformly.
"""

from __future__ import annotations

import asyncio

from stellasaurus.common.clock import Clock, SystemClock
from stellasaurus.common.logging import get_logger
from stellasaurus.common.types import Micros, OutcomePolarity, Side, Venue
from stellasaurus.hot_path.positions import (
    HedgeStatus,
    PaperPosition,
    PositionsStore,
)
from stellasaurus.hot_path.seams import TradeIntent
from stellasaurus.hot_path.state import HotState
from stellasaurus.venues.orders import OrderGateway, OrderResult

_log = get_logger("background.live_execution")


class LiveExecutionEngine:
    """Implements ``seams.Executor``; submit() enqueues, worker executes."""

    def __init__(
        self,
        *,
        state: HotState,
        positions: PositionsStore,
        gateways: dict[Venue, OrderGateway],
        slippage_tolerance_bips: int,
        clock: Clock | None = None,
        requote: object | None = None,  # async (intent) -> (vy, vn) micros or None
    ) -> None:
        self._state = state
        self._positions = positions
        self._gateways = gateways
        self._slip_bips = slippage_tolerance_bips
        self._clock = clock or SystemClock()
        self._requote = requote
        self._queue: asyncio.Queue[TradeIntent] = asyncio.Queue(maxsize=64)
        self._counter = 0

    # --- hot-path side (sync, non-blocking) ---

    def submit(self, intent: TradeIntent) -> None:
        try:
            self._queue.put_nowait(intent)
        except asyncio.QueueFull:
            _log.warning("live_queue_full_intent_dropped", pair_id=intent.pair_id)

    # --- background worker ---

    async def run(self) -> None:
        while True:
            intent = await self._queue.get()
            try:
                await self._execute(intent)
            except Exception as exc:  # noqa: BLE001 - never kill the worker
                _log.error("live_execution_error", pair_id=intent.pair_id, error=str(exc))

    def _limit(self, price: Micros) -> Micros:
        # Pad must survive cent-tick flooring (validated live: a sub-tick pad
        # made FOK require an unmoved book) — floor of 2 ticks.
        pad = max((price * self._slip_bips) // 10_000, 20_000)
        return price + pad

    async def _execute(self, intent: TradeIntent) -> None:
        entry = self._state.registry().by_id.get(intent.pair_id)
        if entry is None:
            return
        self._counter += 1
        position_id = f"live-{intent.pair_id}-{self._clock.wall_ms()}-{self._counter}"

        def leg(venue: Venue) -> tuple[str, OutcomePolarity]:
            if venue is Venue.KALSHI:
                return entry.kalshi_ticker, OutcomePolarity.DIRECT
            return entry.poly_market_slug, entry.outcome_polarity

        yes_native, yes_pol = leg(intent.yes_venue)
        no_native, no_pol = leg(intent.no_venue)

        # Pre-trade re-quote: never fire on in-memory books that may have rotted
        # (found live: event-loop starvation left books minutes stale while
        # feed-level freshness looked fine — two 8.4c unwinds taught this).
        if self._requote is not None:
            fresh = await self._requote(intent)  # type: ignore[operator]
            if fresh is None:
                _log.warning("live_requote_abort", pair_id=intent.pair_id)
                return
            fresh_vy, fresh_vn = fresh
            if fresh_vy + fresh_vn > intent.vwap_yes_micros + intent.vwap_no_micros + 20_000:
                _log.warning(
                    "live_requote_edge_gone", pair_id=intent.pair_id,
                    intent_cost=intent.vwap_yes_micros + intent.vwap_no_micros,
                    fresh_cost=fresh_vy + fresh_vn,
                )
                return
            intent = TradeIntent(
                pair_id=intent.pair_id, orientation=intent.orientation,
                qty=intent.qty, yes_venue=intent.yes_venue, no_venue=intent.no_venue,
                vwap_yes_micros=fresh_vy, vwap_no_micros=fresh_vn,
                net_edge_micros=1_000_000 - (fresh_vy + fresh_vn),
                created_mono_ns=intent.created_mono_ns,
            )

        yes_res, no_res = await asyncio.gather(
            self._gateways[intent.yes_venue].buy_fok(
                native_id=yes_native, side=Side.YES, qty=intent.qty,
                limit_price_micros=self._limit(intent.vwap_yes_micros),
                polarity=yes_pol,
            ),
            self._gateways[intent.no_venue].buy_fok(
                native_id=no_native, side=Side.NO, qty=intent.qty,
                limit_price_micros=self._limit(intent.vwap_no_micros),
                polarity=no_pol,
            ),
            return_exceptions=True,
        )
        yes_ok = isinstance(yes_res, OrderResult) and yes_res.fully_filled
        no_ok = isinstance(no_res, OrderResult) and no_res.fully_filled

        if yes_ok and no_ok:
            fees = (yes_res.fees_micros or 0) + (no_res.fees_micros or 0)  # type: ignore[union-attr]
            vy = yes_res.avg_price_micros or intent.vwap_yes_micros  # type: ignore[union-attr]
            vn = no_res.avg_price_micros or intent.vwap_no_micros  # type: ignore[union-attr]
            self._positions.record(PaperPosition(
                position_id=position_id, pair_id=intent.pair_id,
                orientation=intent.orientation, qty=intent.qty,
                yes_venue=intent.yes_venue, no_venue=intent.no_venue,
                yes_price_micros=vy, no_price_micros=vn, fees_micros=fees,
                committed_micros=intent.qty * (vy + vn) + fees,
                hedge_status=HedgeStatus.HEDGED, unwind_loss_micros=None,
                opened_wall_ms=self._clock.wall_ms(),
                resolves_at_ms=entry.resolves_at_ms,
            ))
            _log.info("live_hedged", pair_id=intent.pair_id, qty=intent.qty)
            return

        if not yes_ok and not no_ok:
            self._positions.record(PaperPosition(
                position_id=position_id, pair_id=intent.pair_id,
                orientation=intent.orientation, qty=intent.qty,
                yes_venue=intent.yes_venue, no_venue=intent.no_venue,
                yes_price_micros=intent.vwap_yes_micros, no_price_micros=None,
                fees_micros=0, committed_micros=0,
                hedge_status=HedgeStatus.FAILED, unwind_loss_micros=None,
                opened_wall_ms=self._clock.wall_ms(),
                resolves_at_ms=entry.resolves_at_ms,
            ))
            return

        # Single leg filled -> forced unwind: sell it back as a marketable
        # order (buy the OPPOSITE side at a crossing price, which nets flat on
        # a single crossed book).
        filled_venue = intent.yes_venue if yes_ok else intent.no_venue
        filled_res: OrderResult = yes_res if yes_ok else no_res  # type: ignore[assignment]
        filled_side = Side.YES if yes_ok else Side.NO
        opposite = Side.NO if yes_ok else Side.YES
        native, pol = leg(filled_venue)
        unwind_loss: Micros | None = None
        try:
            # Marketable: pay up to (1 - 0) — effectively crossing the book.
            unwind = await self._gateways[filled_venue].buy_fok(
                native_id=native, side=opposite, qty=intent.qty,
                limit_price_micros=990_000, polarity=pol,
            )
            buy_px = filled_res.avg_price_micros or 0
            sell_equiv = 1_000_000 - (unwind.avg_price_micros or 990_000)
            unwind_loss = intent.qty * max(0, buy_px - sell_equiv) + (
                (filled_res.fees_micros or 0) + (unwind.fees_micros or 0)
            )
            _log.warning("live_single_leg_unwound", pair_id=intent.pair_id,
                         venue=filled_venue.value, loss_micros=unwind_loss)
        except Exception as exc:  # noqa: BLE001
            _log.error("live_unwind_FAILED_hanging_leg", pair_id=intent.pair_id,
                       venue=filled_venue.value, error=str(exc))
        self._positions.record(PaperPosition(
            position_id=position_id, pair_id=intent.pair_id,
            orientation=intent.orientation, qty=intent.qty,
            yes_venue=intent.yes_venue, no_venue=intent.no_venue,
            yes_price_micros=(filled_res.avg_price_micros or 0)
            if filled_side is Side.YES else intent.vwap_yes_micros,
            no_price_micros=(filled_res.avg_price_micros or 0)
            if filled_side is Side.NO else None,
            fees_micros=filled_res.fees_micros or 0,
            committed_micros=0,
            hedge_status=HedgeStatus.UNWOUND, unwind_loss_micros=unwind_loss,
            opened_wall_ms=self._clock.wall_ms(),
            resolves_at_ms=entry.resolves_at_ms,
        ))
