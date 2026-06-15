"""Money helpers — integer micro-USD arithmetic, never floats.

Prices for binary contracts live in (0, 1) dollars, i.e. (0, 1_000_000) micros.
A contract pays out exactly $1.00 == 1_000_000 micros at resolution.

Floats are banned in book/fee math because rounding errors there cause false
fee-reconciliation divergence (DESIGN §6.4) and incorrect edge calculations.
Conversions from external float/decimal prices happen ONCE, at the venue parse
boundary, via the helpers here.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from stellasaurus.common.types import MICROS_PER_DOLLAR, Micros

PAYOUT_MICROS: Micros = MICROS_PER_DOLLAR  # $1.00 per resolved pair


def dollars_to_micros(value: str | int | Decimal) -> Micros:
    """Convert a dollar amount to integer micro-USD, rounding half-up.

    Accepts str/int/Decimal (NOT float) to avoid binary-float surprises.
    """
    d = value if isinstance(value, Decimal) else Decimal(str(value))
    return int((d * MICROS_PER_DOLLAR).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def cents_to_micros(cents: int) -> Micros:
    """Convert integer cents (e.g. Kalshi prices 1..99) to micro-USD."""
    return cents * 10_000


def micros_to_dollars(micros: Micros) -> Decimal:
    """Exact Decimal dollar value for display/persistence."""
    return (Decimal(micros) / MICROS_PER_DOLLAR).quantize(Decimal("0.000001"))


def micros_to_str(micros: Micros) -> str:
    """Human-friendly dollar string, trimmed to 4 dp."""
    return f"${(Decimal(micros) / MICROS_PER_DOLLAR).quantize(Decimal('0.0001'))}"


def round_to_precision(micros: Micros, precision_micros: Micros) -> Micros:
    """Round a micro amount to a venue balance precision (e.g. $0.01 == 10_000).

    Replicates the venues' balance granularity so locally-computed fees match
    actuals during reconciliation (DESIGN §6.4).
    """
    if precision_micros <= 0:
        raise ValueError("precision_micros must be positive")
    q = Decimal(micros) / Decimal(precision_micros)
    return int(q.quantize(Decimal(1), rounding=ROUND_HALF_UP)) * precision_micros
