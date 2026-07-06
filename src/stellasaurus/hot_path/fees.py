"""Local fee computation (DESIGN §6.4) — never a network call.

GO-REWRITABLE BOUNDARY: stdlib + ``common`` only. All math in Decimal/int; fees
are rounded UP (venue-conservative: overestimating cost can only make the
evaluator more cautious, never less).

Kalshi (event contracts, quadratic):
    fee(order) = round_up(multiplier * C * p * (1 - p))   to balance precision
    e.g. taker multiplier 0.07: 10 contracts @ $0.50 -> 0.07*10*0.25 = $0.175
    -> $0.18 at $0.01 precision. Maker multiplier ~75% lower.

Polymarket US:
    taker fee = max(min_fee, taker_bps/10000 * notional), maker = 0.
    notional = contracts * avg fill price.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal

from stellasaurus.common.types import Micros

_MICRO = Decimal(1_000_000)


@dataclass(frozen=True, slots=True)
class FeeParams:
    """Cached fee parameters (published at startup; background sync refreshes
    them out of band in a later phase via /series/fee_changes and preview)."""

    kalshi_taker_multiplier: Decimal  # e.g. Decimal("0.07")
    kalshi_maker_multiplier: Decimal  # e.g. Decimal("0.0175")
    kalshi_precision_micros: Micros  # $0.01 standard accounts -> 10_000
    poly_taker_bps: int  # 10 -> 0.10% of notional
    poly_maker_bps: int  # 0
    poly_min_fee_micros: Micros  # $0.001 -> 1_000


def _ceil_to(micros: Decimal, precision_micros: Micros) -> Micros:
    if precision_micros <= 0:
        raise ValueError("precision_micros must be positive")
    steps = (micros / precision_micros).quantize(Decimal(1), rounding=ROUND_CEILING)
    return int(steps) * precision_micros


def kalshi_fee_micros(
    contracts: int,
    price_micros: Micros,
    *,
    params: FeeParams,
    is_maker: bool = False,
) -> Micros:
    """Per-ORDER Kalshi fee: multiplier * C * p * (1-p), rounded up to precision."""
    if contracts <= 0:
        return 0
    p = Decimal(price_micros) / _MICRO
    mult = params.kalshi_maker_multiplier if is_maker else params.kalshi_taker_multiplier
    raw_micros = mult * contracts * p * (1 - p) * _MICRO
    return _ceil_to(raw_micros, params.kalshi_precision_micros)


def poly_fee_micros(
    contracts: int,
    price_micros: Micros,
    *,
    params: FeeParams,
    is_maker: bool = False,
) -> Micros:
    """Per-ORDER Polymarket fee: bps of notional with a minimum; maker free."""
    if contracts <= 0:
        return 0
    bps = params.poly_maker_bps if is_maker else params.poly_taker_bps
    if bps == 0:
        return 0
    notional_micros = Decimal(contracts) * Decimal(price_micros)
    raw = notional_micros * bps / Decimal(10_000)
    fee = int(raw.quantize(Decimal(1), rounding=ROUND_CEILING))
    return max(fee, params.poly_min_fee_micros)
