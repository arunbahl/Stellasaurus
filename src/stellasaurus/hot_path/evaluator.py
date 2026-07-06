"""Opportunity Evaluator (DESIGN §6.6) — the hot-path core loop, paper mode.

GO-REWRITABLE BOUNDARY: stdlib + ``common`` + sibling hot_path modules only.
Pure local arithmetic over in-memory books and cached params; no network, no
disk, no LLM. Registered as a ``BookStore`` listener, so it runs event-driven on
every book update for a VERIFIED pair.

Per update it evaluates BOTH orientations (A: Kalshi-YES + Poly-NO,
B: Poly-YES + Kalshi-NO) through the §3.3 gates in order:

    fresh -> books present -> sizing (max_bet_value cap) -> depth (VWAP walk)
    -> net-edge >= theta -> annualized return >= hurdle

and records an ``Opportunity`` either way — the failed gate is as informative as
a fire. Phase 3 is PAPER: a passing evaluation is recorded, never executed
(execution + the risk manager's halt flag arrive in Phase 4).

Note on DERIVED ladders: both venues run a single crossed book per market
(verified live: Polymarket's short-side book is the same book, its BBO shortPx
equals 1 - bestBid), so a NO ladder reflected from YES bids is REAL, executable
liquidity — buying NO at (1-p) matches the YES bid at p.
"""

from __future__ import annotations

from stellasaurus.common.clock import Clock, SystemClock
from stellasaurus.common.money import PAYOUT_MICROS
from stellasaurus.common.types import Micros, Venue
from stellasaurus.hot_path.book import walk_book_for_size
from stellasaurus.hot_path.fees import FeeParams, kalshi_fee_micros, poly_fee_micros
from stellasaurus.hot_path.opportunities import Opportunity, OpportunitySink
from stellasaurus.hot_path.seams import Executor, RiskGate, TradeIntent
from stellasaurus.hot_path.snapshot import LimitsSnapshot
from stellasaurus.hot_path.state import HotState

_DAY_MS = 86_400_000

# (orientation, yes_venue, no_venue)
_ORIENTATIONS: tuple[tuple[str, Venue, Venue], ...] = (
    ("A", Venue.KALSHI, Venue.POLYMARKET),
    ("B", Venue.POLYMARKET, Venue.KALSHI),
)


def _venue_fee(
    venue: Venue, qty: int, price: Micros, params: FeeParams,
    *, kalshi_series: str | None, poly_market: str | None,
) -> Micros:
    if venue is Venue.KALSHI:
        return kalshi_fee_micros(qty, price, params=params, series=kalshi_series)
    return poly_fee_micros(qty, price, params=params, market=poly_market)


def _depth(asks: tuple, cap: int) -> int:  # type: ignore[type-arg]
    total = 0
    for lvl in asks:
        total += lvl.size
        if total >= cap:
            return cap
    return total


class OpportunityEvaluator:
    """Implements the ``seams.Evaluator`` protocol (paper mode)."""

    def __init__(
        self,
        *,
        state: HotState,
        fee_params: FeeParams,
        sink: OpportunitySink,
        risk_gate: RiskGate | None = None,
        executor: Executor | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._state = state
        self._fee_params = fee_params
        self._sink = sink
        self._risk = risk_gate
        self._executor = executor
        self._clock = clock or SystemClock()

    def publish_fee_params(self, params: FeeParams) -> None:
        """Atomic rebind — background fee sync swaps params without locking."""
        self._fee_params = params

    def on_book_update(self, pair_id: str) -> None:
        registry = self._state.registry()
        entry = registry.by_id.get(pair_id)
        if entry is None or pair_id not in registry.verified:
            return
        limits = self._state.limits()
        now_ms = self._clock.wall_ms()
        params = self._fee_params

        fresh = self._state.is_fresh(pair_id)
        t_days: float | None = None
        if entry.resolves_at_ms is not None:
            t_days = max((entry.resolves_at_ms - now_ms) / _DAY_MS, limits.min_t_days)

        best: Opportunity | None = None
        kalshi_series = entry.kalshi_ticker.split("-", 1)[0]
        for orientation, yes_venue, no_venue in _ORIENTATIONS:
            opp = self._evaluate(
                pair_id=pair_id, orientation=orientation,
                yes_venue=yes_venue, no_venue=no_venue,
                fresh=fresh, t_days=t_days, limits=limits,
                params=params, now_ms=now_ms,
                kalshi_series=kalshi_series, poly_market=entry.poly_market_slug,
            )
            self._sink.push(opp)
            if opp.would_fire and (
                best is None or (opp.net_edge_micros or 0) > (best.net_edge_micros or 0)
            ):
                best = opp

        # §6.6: one intent per update — the best passing orientation goes to the
        # risk gate and (paper) executor when Phase 4 components are wired.
        if best is not None and self._risk is not None and self._executor is not None:
            intent = TradeIntent(
                pair_id=best.pair_id,
                orientation=best.orientation,
                qty=best.qty,
                yes_venue=best.yes_venue,
                no_venue=best.no_venue,
                vwap_yes_micros=best.vwap_yes_micros or 0,
                vwap_no_micros=best.vwap_no_micros or 0,
                net_edge_micros=best.net_edge_micros or 0,
                created_mono_ns=self._clock.mono_ns(),
            )
            if self._risk.approve(intent):
                self._executor.submit(intent)

    def _evaluate(  # noqa: PLR0911 - gate ladder reads clearest as early returns
        self, *, pair_id: str, orientation: str, yes_venue: Venue, no_venue: Venue,
        fresh: bool, t_days: float | None, limits: LimitsSnapshot,
        params: FeeParams, now_ms: int,
        kalshi_series: str | None = None, poly_market: str | None = None,
    ) -> Opportunity:
        def blocked(
            gate: str,
            *,
            qty: int = 0,
            vy: Micros | None = None,
            vn: Micros | None = None,
            fees: Micros | None = None,
            net: Micros | None = None,
            committed: Micros | None = None,
            ann: float | None = None,
        ) -> Opportunity:
            return Opportunity(
                pair_id=pair_id, orientation=orientation,
                yes_venue=yes_venue, no_venue=no_venue,
                would_fire=False, gate_failed=gate,
                qty=qty,
                vwap_yes_micros=vy, vwap_no_micros=vn,
                fees_per_pair_micros=fees,
                net_edge_micros=net,
                committed_per_pair_micros=committed,
                t_days=t_days, annualized_return=ann,
                theta_micros=limits.theta_micros, hurdle=limits.hurdle,
                created_wall_ms=now_ms,
            )

        if not fresh:
            return blocked("stale_book")

        yes_book = self._state.book(pair_id, yes_venue)
        no_book = self._state.book(pair_id, no_venue)
        if yes_book is None or no_book is None:
            return blocked("missing_book")
        yes_asks, no_asks = yes_book.yes_asks, no_book.no_asks
        if not yes_asks or not no_asks:
            return blocked("empty_side")

        # Sizing (§6.8): cap Q so committed capital stays under max_bet_value,
        # then by available depth, then by the default target.
        approx_pair_cost = yes_asks[0].price + no_asks[0].price
        if approx_pair_cost <= 0:
            return blocked("empty_side")
        q_cap_capital = int(limits.max_bet_value_micros // approx_pair_cost)
        qty = min(
            limits.target_size_default,
            q_cap_capital,
            _depth(yes_asks, limits.target_size_default),
            _depth(no_asks, limits.target_size_default),
        )
        if qty < 1:
            return blocked("size_zero")

        vy = walk_book_for_size(yes_asks, qty)
        vn = walk_book_for_size(no_asks, qty)
        if vy is None or vn is None:
            return blocked("insufficient_depth", qty=qty)

        fee_yes = _venue_fee(yes_venue, qty, vy, params,
                             kalshi_series=kalshi_series, poly_market=poly_market)
        fee_no = _venue_fee(no_venue, qty, vn, params,
                            kalshi_series=kalshi_series, poly_market=poly_market)
        fees_per_pair = (fee_yes + fee_no + qty - 1) // qty  # ceil per pair
        committed = vy + vn + fees_per_pair
        net = PAYOUT_MICROS - committed

        # Backstop the sizing estimate with the true committed capital.
        if qty * committed > limits.max_bet_value_micros:
            return blocked("max_bet_value", qty=qty, vy=vy, vn=vn,
                           fees=fees_per_pair, net=net, committed=committed)

        if net < limits.theta_micros:
            return blocked("theta", qty=qty, vy=vy, vn=vn,
                           fees=fees_per_pair, net=net, committed=committed)

        ann: float | None = None
        if t_days is not None and committed > 0:
            ann = (net / committed) * (365.0 / t_days)
            if ann < limits.hurdle:
                return blocked("hurdle", qty=qty, vy=vy, vn=vn, fees=fees_per_pair,
                               net=net, committed=committed, ann=ann)

        return Opportunity(
            pair_id=pair_id, orientation=orientation,
            yes_venue=yes_venue, no_venue=no_venue,
            would_fire=True, gate_failed=None,
            qty=qty, vwap_yes_micros=vy, vwap_no_micros=vn,
            fees_per_pair_micros=fees_per_pair, net_edge_micros=net,
            committed_per_pair_micros=committed,
            t_days=t_days, annualized_return=ann,
            theta_micros=limits.theta_micros, hurdle=limits.hurdle,
            created_wall_ms=now_ms,
        )
