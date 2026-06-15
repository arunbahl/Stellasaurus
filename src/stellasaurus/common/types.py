"""Shared enums and tiny value types.

This module is intentionally dependency-free (stdlib only) so that the hot-path
package may import it without pulling in pydantic / asyncio / SDKs.
"""

from __future__ import annotations

from enum import StrEnum


class Venue(StrEnum):
    KALSHI = "KALSHI"
    POLYMARKET = "POLYMARKET"

    @property
    def other(self) -> Venue:
        return Venue.POLYMARKET if self is Venue.KALSHI else Venue.KALSHI


class OutcomePolarity(StrEnum):
    """How each venue's native YES maps onto the canonical proposition.

    DIRECT   -> both venues' native YES == canonical YES.
    INVERTED -> Polymarket's native YES == canonical NO (legs are flipped).
    """

    DIRECT = "DIRECT"
    INVERTED = "INVERTED"


class PairStatus(StrEnum):
    VERIFIED = "VERIFIED"
    NOT_EQUIVALENT = "NOT_EQUIVALENT"
    STALE = "STALE"


class PairSource(StrEnum):
    MANUAL_SEED = "MANUAL_SEED"
    LLM = "LLM"


class Side(StrEnum):
    """Canonical side of a locked pair leg."""

    YES = "YES"
    NO = "NO"


class NoSideSource(StrEnum):
    """Whether a book's NO ladder came from the venue or was synthesized.

    DERIVED ladders (reflected from the YES side) have no independent resting
    liquidity and must never be walked as real depth by the evaluator.
    """

    NATIVE = "NATIVE"
    DERIVED = "DERIVED"


# Canonical money unit across the whole system: integer micro-USD.
# $1.00 == 1_000_000 micros. Chosen so Polymarket's $0.001 min fee and Kalshi's
# $0.0001 direct-member precision are both representable exactly without floats.
Micros = int
MICROS_PER_DOLLAR: int = 1_000_000
