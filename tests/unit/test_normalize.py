"""Tests for canonical-YES normalization — the highest-value pure function.

Covers both venues' native book shapes, polarity mapping, and per-ladder
derivation provenance (NATIVE vs DERIVED).
"""

from stellasaurus.common.types import NoSideSource, OutcomePolarity, Venue
from stellasaurus.hot_path.book import NativeBook, PriceLevel
from stellasaurus.hot_path.normalize import normalize


def _poly_style() -> NativeBook:
    # Polymarket: YES bids + YES asks; NO side implied.
    return NativeBook(
        venue=Venue.POLYMARKET,
        native_id="slug",
        yes_bids=(PriceLevel(590_000, 5),),
        yes_asks=(PriceLevel(610_000, 4),),
        no_bids=None,
        no_asks=None,
        seq=1,
        recv_mono_ns=100,
        recv_wall_ms=200,
    )


def _kalshi_style() -> NativeBook:
    # Kalshi: YES bids + NO bids; asks implied by the opposite side.
    return NativeBook(
        venue=Venue.KALSHI,
        native_id="KX",
        yes_bids=(PriceLevel(590_000, 5),),
        yes_asks=None,
        no_bids=(PriceLevel(420_000, 8),),
        no_asks=None,
        seq=1,
        recv_mono_ns=100,
        recv_wall_ms=200,
    )


def test_polymarket_direct_yes_native_no_derived():
    nb = normalize(_poly_style(), polarity=OutcomePolarity.DIRECT, pair_id="p")
    assert nb.best_yes_ask == PriceLevel(610_000, 4)
    assert nb.yes_side_source is NoSideSource.NATIVE
    # NO ask derived from YES bids: 1 - 0.59 = 0.41
    assert nb.best_no_ask == PriceLevel(410_000, 5)
    assert nb.no_side_source is NoSideSource.DERIVED


def test_kalshi_direct_both_bids_native_asks_derived():
    nb = normalize(_kalshi_style(), polarity=OutcomePolarity.DIRECT, pair_id="p")
    assert nb.best_yes_bid == PriceLevel(590_000, 5)
    assert nb.best_no_bid == PriceLevel(420_000, 8)
    # YES ask derived from NO bids (1 - 0.42 = 0.58); NO ask from YES bids (0.41)
    assert nb.best_yes_ask == PriceLevel(580_000, 8)
    assert nb.best_no_ask == PriceLevel(410_000, 5)
    assert nb.yes_side_source is NoSideSource.DERIVED
    assert nb.no_side_source is NoSideSource.DERIVED


def test_inverted_swaps_yes_and_no():
    # INVERTED: canonical YES == native NO. Poly native NO is implied, so the
    # canonical YES ask is derived from canonical NO bids (native YES bids).
    nb = normalize(_poly_style(), polarity=OutcomePolarity.INVERTED, pair_id="p")
    assert nb.best_no_ask == PriceLevel(610_000, 4)  # native YES ask -> canonical NO ask
    assert nb.no_side_source is NoSideSource.NATIVE
    assert nb.best_yes_ask == PriceLevel(410_000, 5)  # reflected from native YES bids
    assert nb.yes_side_source is NoSideSource.DERIVED


def test_direct_vs_inverted_are_mirror_images():
    direct = normalize(_poly_style(), polarity=OutcomePolarity.DIRECT, pair_id="p")
    inverted = normalize(_poly_style(), polarity=OutcomePolarity.INVERTED, pair_id="p")
    assert direct.best_yes_ask == inverted.best_no_ask
    assert direct.best_no_ask == inverted.best_yes_ask


def test_metadata_preserved():
    nb = normalize(_poly_style(), polarity=OutcomePolarity.DIRECT, pair_id="my-pair")
    assert nb.pair_id == "my-pair"
    assert nb.venue is Venue.POLYMARKET
    assert (nb.seq, nb.recv_mono_ns, nb.recv_wall_ms) == (1, 100, 200)


def test_empty_book_yields_empty_ladders():
    empty = NativeBook(Venue.KALSHI, "x", (), None, (), None, 0, 0, 0)
    nb = normalize(empty, polarity=OutcomePolarity.DIRECT, pair_id="p")
    assert nb.yes_asks == () and nb.no_asks == ()
    assert nb.best_yes_ask is None
