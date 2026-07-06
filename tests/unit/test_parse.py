from stellasaurus.common.types import Venue
from stellasaurus.hot_path.book import PriceLevel
from stellasaurus.venues.kalshi import parse as kparse
from stellasaurus.venues.polymarket import parse as pparse


def test_kalshi_parse_orderbook_cents_to_micros():
    payload = {"orderbook": {"yes": [[59, 5], [58, 10]], "no": [[42, 8]]}}
    nb = kparse.parse_orderbook(
        ticker="KX", payload=payload, seq=1, recv_mono_ns=1, recv_wall_ms=2
    )
    assert nb.venue is Venue.KALSHI
    assert nb.yes_bids == (PriceLevel(590_000, 5), PriceLevel(580_000, 10))
    assert nb.no_bids == (PriceLevel(420_000, 8),)
    assert nb.yes_asks is None and nb.no_asks is None


def test_kalshi_parse_orderbook_skips_zero_size():
    payload = {"orderbook": {"yes": [[59, 0], [58, 3]], "no": []}}
    nb = kparse.parse_orderbook(
        ticker="KX", payload=payload, seq=1, recv_mono_ns=1, recv_wall_ms=2
    )
    assert nb.yes_bids == (PriceLevel(580_000, 3),)


def test_kalshi_parse_orderbook_fp_dollar_strings():
    # Real production shape: fixed-point dollar strings + fractional sizes.
    payload = {
        "orderbook_fp": {
            "yes_dollars": [["0.5900", "254.00"], ["0.5800", "10.50"]],
            "no_dollars": [["0.9820", "12.00"]],
        }
    }
    nb = kparse.parse_orderbook(
        ticker="KX", payload=payload, seq=1, recv_mono_ns=1, recv_wall_ms=2
    )
    assert nb.yes_bids == (PriceLevel(590_000, 254), PriceLevel(580_000, 10))  # 10.50 -> 10
    assert nb.no_bids == (PriceLevel(982_000, 12),)
    assert nb.yes_asks is None and nb.no_asks is None


def test_kalshi_parse_orderbook_fp_empty_side():
    payload = {"orderbook_fp": {"yes_dollars": [], "no_dollars": [["0.9820", "254.00"]]}}
    nb = kparse.parse_orderbook(ticker="KX", payload=payload, seq=1, recv_mono_ns=1, recv_wall_ms=2)
    assert nb.yes_bids == ()
    assert nb.no_bids == (PriceLevel(982_000, 254),)


def test_kalshi_parse_market_fields():
    fields = kparse.parse_market(
        {
            "ticker": "KX1",
            "title": "Will X?",
            "status": "open",
            "close_time": "2026-12-31T00:00:00Z",
        }
    )
    assert fields["native_id"] == "KX1"
    assert fields["resolves_at_ms"] is not None


def test_poly_parse_book_scalar_px():
    payload = {
        "book": {"bids": [{"px": "0.59", "qty": "5"}], "offers": [{"px": "0.61", "qty": "4"}]}
    }
    nb = pparse.parse_book(slug="s", payload=payload, seq=1, recv_mono_ns=1, recv_wall_ms=2)
    assert nb.venue is Venue.POLYMARKET
    assert nb.yes_bids == (PriceLevel(590_000, 5),)
    assert nb.yes_asks == (PriceLevel(610_000, 4),)
    assert nb.no_bids is None and nb.no_asks is None


def test_poly_parse_book_marketdata_wrapper():
    # Live REST shape (verified 2026-07-05): book nested under "marketData",
    # px as an Amount object, fractional qty strings.
    payload = {
        "marketData": {
            "marketSlug": "s",
            "bids": [{"px": {"value": "0.1020", "currency": "USD"}, "qty": "62.0000"}],
            "offers": [{"px": {"value": "0.1370", "currency": "USD"}, "qty": "25.0000"}],
        }
    }
    nb = pparse.parse_book(slug="s", payload=payload, seq=1, recv_mono_ns=1, recv_wall_ms=2)
    assert nb.yes_bids == (PriceLevel(102_000, 62),)
    assert nb.yes_asks == (PriceLevel(137_000, 25),)


def test_poly_parse_book_amount_object_px():
    payload = {"bids": [{"px": {"value": "0.50", "currency": "USD"}, "qty": "2"}], "offers": []}
    nb = pparse.parse_book(slug="s", payload=payload, seq=1, recv_mono_ns=1, recv_wall_ms=2)
    assert nb.yes_bids == (PriceLevel(500_000, 2),)


def test_poly_parse_market_fields():
    fields = pparse.parse_market(
        {"slug": "btc-100k", "title": "BTC?", "endDate": "2026-12-31T00:00:00Z"}
    )
    assert fields["native_id"] == "btc-100k"
    assert fields["resolves_at_ms"] is not None
