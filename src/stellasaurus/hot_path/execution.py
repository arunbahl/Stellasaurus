"""Paper Execution Engine (DESIGN §6.7) — FOK both legs, never a hanging leg.

GO-REWRITABLE BOUNDARY: stdlib + ``common`` + sibling hot_path modules only.

Simulates the default taker policy against the CURRENT in-memory books:

  * Each leg "fills" iff the book can still deliver the intent's quantity at a
    VWAP no worse than intent price + slippage tolerance (FOK semantics — the
    book may have moved since the evaluator saw it).
  * Both legs fill  -> HEDGED position, capital committed until resolution.
  * Exactly one leg -> forced unwind: sell the filled leg back at the current
    bid VWAP; the realized loss (spread + fees both ways) is recorded. This is
    the §6.7 guarantee: fully hedged or fully flat.
  * Neither leg     -> FAILED record, nothing held.

Real venue order submission arrives behind this same ``seams.Executor``
interface later; nothing here talks to a network.
"""

from __future__ import annotations

from stellasaurus.common.clock import Clock, SystemClock
from stellasaurus.common.types import Micros, Venue
from stellasaurus.hot_path.book import NormalizedBook, walk_book_for_size
from stellasaurus.hot_path.fees import FeeParams, kalshi_fee_micros, poly_fee_micros
from stellasaurus.hot_path.positions import (
    HedgeStatus,
    PaperPosition,
    PositionsStore,
)
from stellasaurus.hot_path.seams import TradeIntent
from stellasaurus.hot_path.state import HotState


def _fee(venue: Venue, qty: int, price: Micros, params: FeeParams) -> Micros:
    if venue is Venue.KALSHI:
        return kalshi_fee_micros(qty, price, params=params)
    return poly_fee_micros(qty, price, params=params)


def _leg_fill(
    book: NormalizedBook | None, *, side_asks: bool, qty: int,
    limit_micros: Micros,
) -> Micros | None:
    """VWAP if the current book fills ``qty`` within ``limit_micros``, else None."""
    if book is None:
        return None
    ladder = book.yes_asks if side_asks else book.no_asks
    vwap = walk_book_for_size(ladder, qty)
    if vwap is None or vwap > limit_micros:
        return None
    return vwap


def _bid_vwap(book: NormalizedBook | None, *, yes_side: bool, qty: int) -> Micros:
    """Sell-back VWAP on the current bids; 0 if no depth (total-loss floor)."""
    if book is None:
        return 0
    ladder = book.yes_bids if yes_side else book.no_bids
    return walk_book_for_size(ladder, qty) or 0


class PaperExecutionEngine:
    """Implements the ``seams.Executor`` protocol (paper mode)."""

    def __init__(
        self,
        *,
        state: HotState,
        positions: PositionsStore,
        fee_params: FeeParams,
        slippage_tolerance_bips: int,
        clock: Clock | None = None,
    ) -> None:
        self._state = state
        self._positions = positions
        self._fee_params = fee_params
        self._slip_bips = slippage_tolerance_bips
        self._clock = clock or SystemClock()
        self._counter = 0

    def _limit(self, intent_price: Micros) -> Micros:
        return intent_price + (intent_price * self._slip_bips) // 10_000

    def submit(self, intent: TradeIntent) -> None:
        params = self._fee_params
        now_ms = self._clock.wall_ms()
        self._counter += 1
        position_id = f"paper-{intent.pair_id}-{now_ms}-{self._counter}"
        entry = self._state.registry().by_id.get(intent.pair_id)
        resolves = entry.resolves_at_ms if entry else None

        yes_book = self._state.book(intent.pair_id, intent.yes_venue)
        no_book = self._state.book(intent.pair_id, intent.no_venue)
        vy = _leg_fill(yes_book, side_asks=True, qty=intent.qty,
                       limit_micros=self._limit(intent.vwap_yes_micros))
        vn = _leg_fill(no_book, side_asks=False, qty=intent.qty,
                       limit_micros=self._limit(intent.vwap_no_micros))

        if vy is not None and vn is not None:
            fees = (
                _fee(intent.yes_venue, intent.qty, vy, params)
                + _fee(intent.no_venue, intent.qty, vn, params)
            )
            self._positions.record(PaperPosition(
                position_id=position_id, pair_id=intent.pair_id,
                orientation=intent.orientation, qty=intent.qty,
                yes_venue=intent.yes_venue, no_venue=intent.no_venue,
                yes_price_micros=vy, no_price_micros=vn,
                fees_micros=fees,
                committed_micros=intent.qty * (vy + vn) + fees,
                hedge_status=HedgeStatus.HEDGED, unwind_loss_micros=None,
                opened_wall_ms=now_ms, resolves_at_ms=resolves,
            ))
            return

        if vy is None and vn is None:
            self._positions.record(PaperPosition(
                position_id=position_id, pair_id=intent.pair_id,
                orientation=intent.orientation, qty=intent.qty,
                yes_venue=intent.yes_venue, no_venue=intent.no_venue,
                yes_price_micros=intent.vwap_yes_micros, no_price_micros=None,
                fees_micros=0, committed_micros=0,
                hedge_status=HedgeStatus.FAILED, unwind_loss_micros=None,
                opened_wall_ms=now_ms, resolves_at_ms=resolves,
            ))
            return

        # Exactly one leg filled -> forced unwind of that leg (§6.7 / §10).
        if vy is not None:
            filled_venue, filled_price, yes_side = intent.yes_venue, vy, True
            book = yes_book
        else:
            filled_venue, filled_price, yes_side = intent.no_venue, vn, False  # type: ignore[assignment]
            book = no_book
        sell_vwap = _bid_vwap(book, yes_side=yes_side, qty=intent.qty)
        buy_fee = _fee(filled_venue, intent.qty, filled_price, params)
        sell_fee = _fee(filled_venue, intent.qty, sell_vwap, params) if sell_vwap else 0
        loss = intent.qty * (filled_price - sell_vwap) + buy_fee + sell_fee
        self._positions.record(PaperPosition(
            position_id=position_id, pair_id=intent.pair_id,
            orientation=intent.orientation, qty=intent.qty,
            yes_venue=intent.yes_venue, no_venue=intent.no_venue,
            yes_price_micros=filled_price if yes_side else intent.vwap_yes_micros,
            no_price_micros=None if yes_side else filled_price,
            fees_micros=buy_fee + sell_fee,
            committed_micros=0,
            hedge_status=HedgeStatus.UNWOUND, unwind_loss_micros=loss,
            opened_wall_ms=now_ms, resolves_at_ms=resolves,
        ))
