"""Order-book value types and pure book math.

GO-REWRITABLE BOUNDARY: this module imports only stdlib + ``common`` (pure).
No asyncio, no SDKs, no SQLite, no pydantic.

``NativeBook`` is venue-native terms (native YES/NO, micro-USD prices) and is what
the venue adapters produce. ``NormalizedBook`` is canonical-YES terms and is what
the hot path stores and the evaluator consumes. Conversion is in ``normalize.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from stellasaurus.common.money import PAYOUT_MICROS
from stellasaurus.common.types import Micros, NoSideSource, Venue


@dataclass(frozen=True, slots=True)
class PriceLevel:
    """A single resting level. ``price`` is per-contract micro-USD in (0, 1e6)."""

    price: Micros
    size: int  # contracts available at this level


@dataclass(frozen=True, slots=True)
class NativeBook:
    """A venue's book in its OWN native YES/NO terms.

    ``no_*`` ladders are ``None`` when the venue does not natively publish them;
    the normalizer will derive them by reflection.
    """

    venue: Venue
    native_id: str
    yes_bids: tuple[PriceLevel, ...]
    yes_asks: tuple[PriceLevel, ...]
    no_bids: tuple[PriceLevel, ...] | None
    no_asks: tuple[PriceLevel, ...] | None
    seq: int
    recv_mono_ns: int
    recv_wall_ms: int


@dataclass(frozen=True, slots=True)
class NormalizedBook:
    """A book mapped onto the pair's canonical proposition (canonical-YES, USD).

    A price comparison across two ``NormalizedBook``\\s for the same ``pair_id`` is
    apples-to-apples regardless of each venue's native polarity.
    """

    venue: Venue
    pair_id: str
    yes_bids: tuple[PriceLevel, ...]  # sorted by price DESC (best bid first)
    yes_asks: tuple[PriceLevel, ...]  # sorted by price ASC  (best ask first)
    no_bids: tuple[PriceLevel, ...]
    no_asks: tuple[PriceLevel, ...]
    yes_side_source: NoSideSource
    no_side_source: NoSideSource
    seq: int
    recv_mono_ns: int
    recv_wall_ms: int

    # --- BBO convenience (top of ladder; None if empty) ---
    @property
    def best_yes_bid(self) -> PriceLevel | None:
        return self.yes_bids[0] if self.yes_bids else None

    @property
    def best_yes_ask(self) -> PriceLevel | None:
        return self.yes_asks[0] if self.yes_asks else None

    @property
    def best_no_bid(self) -> PriceLevel | None:
        return self.no_bids[0] if self.no_bids else None

    @property
    def best_no_ask(self) -> PriceLevel | None:
        return self.no_asks[0] if self.no_asks else None


def reflect(levels: tuple[PriceLevel, ...]) -> tuple[PriceLevel, ...]:
    """Reflect a YES ladder into the complementary NO ladder (or vice versa).

    For a binary contract YES + NO == $1, so a YES order at price ``p`` is the
    same economic order as a NO order at ``1 - p`` with identical size:

        yes_bid @ p   <=>  no_ask @ (1 - p)
        yes_ask @ p   <=>  no_bid @ (1 - p)

    The caller is responsible for re-sorting the result for the target side.
    """
    return tuple(PriceLevel(price=PAYOUT_MICROS - lvl.price, size=lvl.size) for lvl in levels)


def sort_bids(levels: tuple[PriceLevel, ...]) -> tuple[PriceLevel, ...]:
    return tuple(sorted(levels, key=lambda lvl: lvl.price, reverse=True))


def sort_asks(levels: tuple[PriceLevel, ...]) -> tuple[PriceLevel, ...]:
    return tuple(sorted(levels, key=lambda lvl: lvl.price))


def walk_book_for_size(asks: tuple[PriceLevel, ...], qty: int) -> Micros | None:
    """Volume-weighted average price (micro-USD/contract) to BUY ``qty`` contracts.

    Walks the ask ladder (assumed sorted best-first) accumulating depth. Returns
    ``None`` if there is not enough resting depth to fill ``qty`` — i.e. the depth
    gate fails (DESIGN §3.4 / §6.6). VWAP folds slippage into the price directly.
    """
    if qty <= 0:
        raise ValueError("qty must be positive")
    remaining = qty
    total_cost = 0  # micro-USD * contracts
    for lvl in asks:
        take = min(remaining, lvl.size)
        total_cost += take * lvl.price
        remaining -= take
        if remaining == 0:
            vwap = Decimal(total_cost) / Decimal(qty)
            return int(vwap.quantize(Decimal(1), rounding=ROUND_HALF_UP))
    return None  # insufficient depth
