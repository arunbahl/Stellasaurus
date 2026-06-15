"""Normalize a venue-native book onto a pair's canonical proposition.

The single most safety-relevant pure function in the hot path: it makes a price on
Kalshi and a price on Polymarket directly comparable by applying the pair's
``outcome_polarity`` and deriving any ladder a venue doesn't directly quote.

Venues quote different ladders:
  * Kalshi returns YES *bids* and NO *bids* (asks are implied by the opposite
    side: a NO bid @ q is a YES ask @ 1 - q).
  * Polymarket returns YES bids and YES asks (the NO side is implied).

So we derive each of the four canonical ladders per-ladder from the best
available complement via reflection (YES + NO == $1). A ladder obtained by
reflection is tagged ``DERIVED`` (provenance / audit); a directly-quoted ladder
is ``NATIVE``. The flags describe the *ask* ladders the evaluator walks to buy.

GO-REWRITABLE BOUNDARY: stdlib + ``common`` + sibling ``book`` only.
"""

from __future__ import annotations

from stellasaurus.common.types import NoSideSource, OutcomePolarity
from stellasaurus.hot_path.book import (
    NativeBook,
    NormalizedBook,
    PriceLevel,
    reflect,
    sort_asks,
    sort_bids,
)

_Ladder = tuple[PriceLevel, ...] | None


def normalize(
    native: NativeBook,
    *,
    polarity: OutcomePolarity,
    pair_id: str,
) -> NormalizedBook:
    c_yes_bids: _Ladder
    c_yes_asks: _Ladder
    c_no_bids: _Ladder
    c_no_asks: _Ladder
    if polarity is OutcomePolarity.DIRECT:
        c_yes_bids, c_yes_asks = native.yes_bids, native.yes_asks
        c_no_bids, c_no_asks = native.no_bids, native.no_asks
    else:  # INVERTED: canonical YES is the native NO side, and vice versa.
        c_yes_bids, c_yes_asks = native.no_bids, native.no_asks
        c_no_bids, c_no_asks = native.yes_bids, native.yes_asks

    yes_asks, yes_src = _derive_ask(direct=c_yes_asks, complement_bids=c_no_bids)
    no_asks, no_src = _derive_ask(direct=c_no_asks, complement_bids=c_yes_bids)
    yes_bids = _derive_bid(direct=c_yes_bids, complement_asks=c_no_asks)
    no_bids = _derive_bid(direct=c_no_bids, complement_asks=c_yes_asks)

    return NormalizedBook(
        venue=native.venue,
        pair_id=pair_id,
        yes_bids=yes_bids,
        yes_asks=yes_asks,
        no_bids=no_bids,
        no_asks=no_asks,
        yes_side_source=yes_src,
        no_side_source=no_src,
        seq=native.seq,
        recv_mono_ns=native.recv_mono_ns,
        recv_wall_ms=native.recv_wall_ms,
    )


def _derive_ask(
    *, direct: _Ladder, complement_bids: _Ladder
) -> tuple[tuple[PriceLevel, ...], NoSideSource]:
    """Ask ladder for a side: use the quoted asks, else reflect the opposite
    side's bids (a complement bid @ q is this side's ask @ 1 - q)."""
    if direct is not None:
        return sort_asks(direct), NoSideSource.NATIVE
    if complement_bids is not None:
        return sort_asks(reflect(complement_bids)), NoSideSource.DERIVED
    return (), NoSideSource.NATIVE


def _derive_bid(*, direct: _Ladder, complement_asks: _Ladder) -> tuple[PriceLevel, ...]:
    """Bid ladder for a side: use the quoted bids, else reflect the opposite
    side's asks (a complement ask @ q is this side's bid @ 1 - q)."""
    if direct is not None:
        return sort_bids(direct)
    if complement_asks is not None:
        return sort_bids(reflect(complement_asks))
    return ()
