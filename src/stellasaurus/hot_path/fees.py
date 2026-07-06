"""Local fee computation (DESIGN §6.4) — never a network call.

GO-REWRITABLE BOUNDARY: stdlib + ``common`` only. All math in Decimal/int; fees
are rounded UP (venue-conservative: overestimating cost can only make the
evaluator more cautious, never less).

Kalshi (event contracts, quadratic):
    fee(order) = round_up(multiplier * C * p * (1 - p))   to balance precision
    e.g. taker multiplier 0.07: 10 contracts @ $0.50 -> 0.07*10*0.25 = $0.175
    -> $0.18 at $0.01 precision. Maker multiplier ~75% lower. (Verified live
    2026-07-06: /series/fee_changes has no per-series overrides scheduled.)

Polymarket US (VERIFIED against docs.polymarket.us/fees 2026-07-06 — the venue
uses a QUADRATIC schedule, not the flat bps DESIGN.md assumed):
    taker fee   = 0.06    * C * p * (1 - p)   (per-market feeCoefficient)
    maker REBATE= -0.0125 * C * p * (1 - p)   (negative — paid to the maker)
    rounded to the cent with banker's rounding (round half to even).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_HALF_EVEN, Decimal

from stellasaurus.common.types import Micros

_MICRO = Decimal(1_000_000)
_CENT = Decimal(10_000)  # micros per cent


@dataclass(frozen=True, slots=True)
class FeeParams:
    """Cached fee parameters (published at startup; background sync refreshes
    them out of band in a later phase via /series/fee_changes and preview).

    Polymarket's coefficient is per-market (``feeCoefficient`` in the catalog);
    this default applies when a market-specific value isn't wired through yet.
    """

    kalshi_taker_multiplier: Decimal  # e.g. Decimal("0.07")
    kalshi_maker_multiplier: Decimal  # e.g. Decimal("0.0175")
    kalshi_precision_micros: Micros  # $0.01 standard accounts -> 10_000
    poly_taker_coefficient: Decimal  # e.g. Decimal("0.06")
    poly_maker_coefficient: Decimal  # e.g. Decimal("-0.0125") — a rebate


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
    """Per-ORDER Polymarket fee: coefficient * C * p * (1-p), banker's-rounded
    to the cent. Negative for makers (rebate)."""
    if contracts <= 0:
        return 0
    p = Decimal(price_micros) / _MICRO
    coeff = params.poly_maker_coefficient if is_maker else params.poly_taker_coefficient
    raw_micros = coeff * contracts * p * (1 - p) * _MICRO
    cents = (raw_micros / _CENT).quantize(Decimal(1), rounding=ROUND_HALF_EVEN)
    return int(cents) * int(_CENT)
