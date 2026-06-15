"""Protocols reserving the hot-path seams for later phases.

Defining these now keeps the spine stable: Phases 3-4 supply implementations and
wire them in the composition root without refactoring ingestion or the snapshot
boundary. None are implemented or invoked in Phase 1.

GO-REWRITABLE BOUNDARY: stdlib + ``common`` + sibling hot_path modules only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from stellasaurus.common.types import Micros, Side, Venue


@dataclass(frozen=True, slots=True)
class TradeIntent:
    """Output of the evaluator; input to risk gate + executor (DESIGN §6.6)."""

    pair_id: str
    orientation: str  # "A" | "B"
    qty: int
    yes_venue: Venue
    no_venue: Venue
    vwap_yes_micros: Micros
    vwap_no_micros: Micros
    net_edge_micros: Micros
    created_mono_ns: int


class FeeEngine(Protocol):
    """Local, exact fee computation (DESIGN §6.4). Phase 3."""

    def fee_micros(
        self, venue: Venue, contracts: int, price_micros: Micros, *, side: Side, is_maker: bool
    ) -> Micros: ...


class Evaluator(Protocol):
    """Event-driven opportunity evaluator (DESIGN §6.6). Phase 3.

    Registered as a ``BookStore`` listener; invoked with the affected pair_id on
    every book update. Pure local arithmetic over in-memory state.
    """

    def on_book_update(self, pair_id: str) -> None: ...


class RiskGate(Protocol):
    """Risk / position / capital approval (DESIGN §6.8). Phase 4."""

    def approve(self, intent: TradeIntent) -> bool: ...


class Executor(Protocol):
    """Order placement with leg-risk guarantees (DESIGN §6.7). Phase 4.

    Phase 1 ships only a no-op / paper implementation; real submission stays
    behind ``Settings.live_trading_enabled``.
    """

    def submit(self, intent: TradeIntent) -> None: ...
