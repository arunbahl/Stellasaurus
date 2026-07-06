"""Risk / Position / Capital Manager (DESIGN §6.8) — the hot-path approve() gate.

GO-REWRITABLE BOUNDARY: stdlib + ``common`` + sibling hot_path modules only.

``approve()`` is the last line before execution. Checks, in order: halt flag
clear, pair still VERIFIED and fresh, no duplicate open position on the pair,
committed capital within the pool, open-pair count within limits, and the
``max_bet_value`` backstop (independent of evaluator sizing, per §6.8).

Every decision — approved or rejected, with the failed check — is recorded to an
in-memory deque that a background task drains to the audit log.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass

from stellasaurus.common.clock import Clock, SystemClock
from stellasaurus.common.types import Micros
from stellasaurus.hot_path.positions import PositionsStore
from stellasaurus.hot_path.seams import TradeIntent
from stellasaurus.hot_path.state import HotState


@dataclass(frozen=True, slots=True)
class RiskDecision:
    pair_id: str
    orientation: str
    qty: int
    committed_micros: Micros
    approved: bool
    rejected_by: str | None
    ts_wall_ms: int


class RiskManager:
    """Implements the ``seams.RiskGate`` protocol."""

    def __init__(
        self,
        *,
        state: HotState,
        positions: PositionsStore,
        clock: Clock | None = None,
        decisions_maxlen: int = 500,
    ) -> None:
        self._state = state
        self._positions = positions
        self._clock = clock or SystemClock()
        self._decisions: deque[RiskDecision] = deque(maxlen=decisions_maxlen)
        self._undrained: deque[RiskDecision] = deque(maxlen=decisions_maxlen)
        self._lock = threading.Lock()
        # In-flight reservations: intents approved but not yet recorded as
        # positions. Execution is async (evaluator -> queue -> worker), so
        # without this the evaluator floods the queue before the first position
        # lands and every gate sees an empty store — validated live: 13 intents
        # cleared a max_open_pairs=1 gate. reserve() is synchronous within the
        # single-tick evaluator->approve->submit chain, closing the race.
        self._reserved: dict[str, Micros] = {}

    def approve(self, intent: TradeIntent) -> bool:
        committed = intent.qty * (intent.vwap_yes_micros + intent.vwap_no_micros)
        with self._lock:
            rejected_by = self._check(intent, committed)
            if rejected_by is None:
                # Reserve synchronously — before the tick yields to the executor
                # worker — so the next evaluator fire counts this in-flight leg.
                self._reserved[intent.pair_id] = committed
            decision = RiskDecision(
                pair_id=intent.pair_id,
                orientation=intent.orientation,
                qty=intent.qty,
                committed_micros=committed,
                approved=rejected_by is None,
                rejected_by=rejected_by,
                ts_wall_ms=self._clock.wall_ms(),
            )
            self._decisions.append(decision)
            self._undrained.append(decision)
        return rejected_by is None

    def release(self, pair_id: str) -> None:
        """Drop a reservation once the executor has recorded the outcome. Called
        for EVERY terminal outcome (hedged/unwound/failed/hanging) so a slot is
        never leaked. Idempotent."""
        with self._lock:
            self._reserved.pop(pair_id, None)

    def _check(self, intent: TradeIntent, committed: Micros) -> str | None:
        # NOTE: caller holds self._lock (reservations read here must be atomic
        # with the reserve that follows a pass).
        limits = self._state.limits()
        if limits.halted:
            return "halted"
        registry = self._state.registry()
        if intent.pair_id not in registry.verified:
            return "pair_not_verified"
        if not self._state.is_fresh(intent.pair_id):
            return "stale_book"
        # a recorded position OR an in-flight reservation counts as "open"
        if self._positions.has_open(intent.pair_id) or intent.pair_id in self._reserved:
            return "pair_already_open"
        if committed > limits.max_bet_value_micros:
            return "max_bet_value"
        totals = self._positions.totals()
        reserved_pairs = len(self._reserved)
        reserved_committed = sum(self._reserved.values())
        if totals.open_pairs + reserved_pairs + 1 > limits.max_open_pairs:
            return "max_open_pairs"
        effective = totals.committed_micros + reserved_committed + committed
        if effective > limits.max_committed_capital_micros:
            return "max_committed_capital"
        if effective > limits.max_aggregate_exposure_micros:
            return "max_aggregate_exposure"
        return None

    def decisions(self) -> tuple[RiskDecision, ...]:
        with self._lock:
            return tuple(self._decisions)

    def drain_decisions(self) -> tuple[RiskDecision, ...]:
        with self._lock:
            out = tuple(self._undrained)
            self._undrained.clear()
        return out
