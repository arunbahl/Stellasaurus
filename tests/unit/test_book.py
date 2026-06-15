import pytest

from stellasaurus.hot_path.book import (
    PriceLevel,
    reflect,
    sort_asks,
    sort_bids,
    walk_book_for_size,
)


def test_walk_book_vwap():
    asks = (PriceLevel(600_000, 5), PriceLevel(610_000, 10))
    # fill 8: 5@600000 + 3@610000 = 4_830_000 / 8 = 603_750
    assert walk_book_for_size(asks, 8) == 603_750


def test_walk_book_exact_top_level():
    asks = (PriceLevel(600_000, 5),)
    assert walk_book_for_size(asks, 5) == 600_000


def test_walk_book_insufficient_depth_returns_none():
    asks = (PriceLevel(600_000, 5), PriceLevel(610_000, 10))
    assert walk_book_for_size(asks, 999) is None


def test_walk_book_empty_returns_none():
    assert walk_book_for_size((), 1) is None


def test_walk_book_rejects_nonpositive_qty():
    with pytest.raises(ValueError):
        walk_book_for_size((PriceLevel(1, 1),), 0)


def test_reflect_is_complement():
    # YES bid @ 0.59 <=> NO ask @ 0.41, size preserved
    out = reflect((PriceLevel(590_000, 7),))
    assert out == (PriceLevel(410_000, 7),)


def test_sort_helpers():
    levels = (PriceLevel(2, 1), PriceLevel(5, 1), PriceLevel(1, 1))
    assert [lvl.price for lvl in sort_bids(levels)] == [5, 2, 1]
    assert [lvl.price for lvl in sort_asks(levels)] == [1, 2, 5]
