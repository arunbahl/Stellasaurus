"""In-memory paper position store (DESIGN §6.8 state).

GO-REWRITABLE BOUNDARY: stdlib + ``common`` only.

The hot path reads/writes positions here (never disk); a background task drains
newly opened/changed positions to the SQLite ``positions`` table. Committed
capital is locked until resolution (§6.8): totals include every non-flat
position.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, replace
from enum import StrEnum

from stellasaurus.common.types import Micros, Venue


class HedgeStatus(StrEnum):
    HEDGED = "HEDGED"  # both legs filled — safe, held to resolution
    UNWOUND = "UNWOUND"  # single leg filled, immediately flattened (loss taken)
    FAILED = "FAILED"  # neither leg filled — nothing held


@dataclass(frozen=True, slots=True)
class PaperPosition:
    position_id: str
    pair_id: str
    orientation: str
    qty: int
    yes_venue: Venue
    no_venue: Venue
    yes_price_micros: Micros  # realized VWAP per contract
    no_price_micros: Micros | None  # None when the NO leg never filled
    fees_micros: Micros  # total realized fees, both legs incl. any unwind
    committed_micros: Micros  # total capital locked (0 for UNWOUND/FAILED)
    hedge_status: HedgeStatus
    unwind_loss_micros: Micros | None  # realized loss on a single-leg unwind
    opened_wall_ms: int
    resolves_at_ms: int | None


@dataclass(frozen=True, slots=True)
class PositionTotals:
    open_pairs: int
    committed_micros: Micros
    exposure_by_venue: tuple[tuple[Venue, Micros], ...]
    unwind_count: int
    unwind_loss_micros: Micros


class PositionsStore:
    """Single-writer (executor) / many-reader. Lock held for dict ops only."""

    def __init__(self) -> None:
        self._by_id: dict[str, PaperPosition] = {}
        self._undrained: list[PaperPosition] = []
        self._lock = threading.Lock()

    def record(self, position: PaperPosition) -> None:
        with self._lock:
            self._by_id[position.position_id] = position
            self._undrained.append(position)

    def has_open(self, pair_id: str) -> bool:
        with self._lock:
            return any(
                p.pair_id == pair_id and p.hedge_status is HedgeStatus.HEDGED
                for p in self._by_id.values()
            )

    def totals(self) -> PositionTotals:
        with self._lock:
            hedged = [p for p in self._by_id.values() if p.hedge_status is HedgeStatus.HEDGED]
            unwound = [p for p in self._by_id.values() if p.hedge_status is HedgeStatus.UNWOUND]
            expo: dict[Venue, int] = {}
            for p in hedged:
                expo[p.yes_venue] = expo.get(p.yes_venue, 0) + p.qty * p.yes_price_micros
                if p.no_price_micros is not None:
                    expo[p.no_venue] = expo.get(p.no_venue, 0) + p.qty * p.no_price_micros
            return PositionTotals(
                open_pairs=len(hedged),
                committed_micros=sum(p.committed_micros for p in hedged),
                exposure_by_venue=tuple(expo.items()),
                unwind_count=len(unwound),
                unwind_loss_micros=sum(p.unwind_loss_micros or 0 for p in unwound),
            )

    def resolve_expired(self, now_ms: int) -> tuple[PaperPosition, ...]:
        """Mark hedged positions past resolution as closed (payout settles
        elsewhere in a later phase); returns the resolved positions."""
        out: list[PaperPosition] = []
        with self._lock:
            for pid, p in list(self._by_id.items()):
                if (
                    p.hedge_status is HedgeStatus.HEDGED
                    and p.resolves_at_ms is not None
                    and p.resolves_at_ms <= now_ms
                ):
                    del self._by_id[pid]
                    out.append(p)
        return tuple(out)

    def open_positions(self) -> tuple[PaperPosition, ...]:
        with self._lock:
            return tuple(self._by_id.values())

    def drain_new(self) -> tuple[PaperPosition, ...]:
        with self._lock:
            out = tuple(self._undrained)
            self._undrained.clear()
        return out


def flatten_position(p: PaperPosition, *, loss_micros: Micros) -> PaperPosition:
    return replace(
        p,
        hedge_status=HedgeStatus.UNWOUND,
        committed_micros=0,
        unwind_loss_micros=loss_micros,
    )
