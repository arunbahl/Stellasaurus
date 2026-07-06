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


@dataclass(frozen=True, slots=True)
class _Reservation:
    committed: Micros   # fee-padded estimate counted against the capital pool
    created_ms: int     # for the in-flight TTL safety net
    held: bool          # HANGING hold: exempt from TTL, freed only by the flattener


class RiskManager:
    """Implements the ``seams.RiskGate`` protocol."""

    def __init__(
        self,
        *,
        state: HotState,
        positions: PositionsStore,
        clock: Clock | None = None,
        decisions_maxlen: int = 500,
        cooldown_ms: int = 30_000,
        reservation_ttl_ms: int = 30_000,
        fee_headroom_bps: int = 500,
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
        self._reserved: dict[str, _Reservation] = {}
        # Per-pair cooldown after a non-hedged outcome, so a pair that keeps
        # half-filling can't be re-fired every tick into a loss-churn loop.
        self._cooldown_until: dict[str, int] = {}
        self._cooldown_ms = cooldown_ms
        # TTL safety net: a non-held reservation older than this is presumed
        # orphaned (its executor died before recording) and purged, so a leak
        # can never permanently wedge the gate. Far above real execution (~2s).
        self._ttl_ms = reservation_ttl_ms
        # Pad the reserved capital estimate so the pool gate doesn't under-count
        # fees while an intent is in flight (committed excludes venue fees).
        self._fee_headroom_bps = fee_headroom_bps

    def approve(self, intent: TradeIntent) -> bool:
        committed = intent.qty * (intent.vwap_yes_micros + intent.vwap_no_micros)
        reserved_amt = committed + committed * self._fee_headroom_bps // 10_000
        now = self._clock.wall_ms()
        with self._lock:
            self._purge_expired(now)
            rejected_by = self._check(intent, committed, reserved_amt, now)
            if rejected_by is None:
                # Reserve synchronously — before the tick yields to the executor
                # worker — so the next evaluator fire counts this in-flight leg.
                self._reserved[intent.pair_id] = _Reservation(
                    committed=reserved_amt, created_ms=now, held=False
                )
            decision = RiskDecision(
                pair_id=intent.pair_id,
                orientation=intent.orientation,
                qty=intent.qty,
                committed_micros=committed,
                approved=rejected_by is None,
                rejected_by=rejected_by,
                ts_wall_ms=now,
            )
            self._decisions.append(decision)
            self._undrained.append(decision)
        return rejected_by is None

    def release(self, pair_id: str) -> None:
        """Drop a reservation once the executor has recorded a CLEAN outcome, or
        once the flattener confirms a HANGING leg flat. Idempotent."""
        with self._lock:
            self._reserved.pop(pair_id, None)

    def mark_held(self, pair_id: str) -> None:
        """Convert a reservation to a HANGING hold: exempt from the TTL purge so
        a real naked leg's slot/capital stays counted until the flattener frees
        it. Without this the TTL would eventually make the naked leg invisible."""
        with self._lock:
            r = self._reserved.get(pair_id)
            if r is not None and not r.held:
                self._reserved[pair_id] = _Reservation(r.committed, r.created_ms, True)

    def reprice(self, pair_id: str, committed: Micros) -> None:
        """Update a reservation's capital estimate to fresh (post-requote) prices
        so the pool gate isn't checked against a stale figure while in flight."""
        reserved_amt = committed + committed * self._fee_headroom_bps // 10_000
        with self._lock:
            r = self._reserved.get(pair_id)
            if r is not None:
                self._reserved[pair_id] = _Reservation(reserved_amt, r.created_ms, r.held)

    def cooldown(self, pair_id: str) -> None:
        """Start a re-entry cooldown for a pair after a non-hedged outcome
        (UNWOUND/FAILED), damping loss-churn on a chronically half-filling pair."""
        with self._lock:
            self._cooldown_until[pair_id] = self._clock.wall_ms() + self._cooldown_ms

    def _purge_expired(self, now: int) -> None:
        # caller holds the lock
        stale = [
            pid for pid, r in self._reserved.items()
            if not r.held and now - r.created_ms > self._ttl_ms
        ]
        for pid in stale:
            del self._reserved[pid]

    def _check(
        self, intent: TradeIntent, committed: Micros, reserved_amt: Micros, now: int
    ) -> str | None:
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
        if now < self._cooldown_until.get(intent.pair_id, 0):
            return "cooldown"
        # a recorded position OR an in-flight reservation counts as "open"
        if self._positions.has_open(intent.pair_id) or intent.pair_id in self._reserved:
            return "pair_already_open"
        if committed > limits.max_bet_value_micros:
            return "max_bet_value"
        totals = self._positions.totals()
        reserved_pairs = len(self._reserved)
        reserved_committed = sum(r.committed for r in self._reserved.values())
        if totals.open_pairs + reserved_pairs + 1 > limits.max_open_pairs:
            return "max_open_pairs"
        effective = totals.committed_micros + reserved_committed + reserved_amt
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
