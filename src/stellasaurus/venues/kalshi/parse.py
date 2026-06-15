"""Parse Kalshi JSON into venue-neutral DTOs.

NOTE: exact field availability for trade-api/v2 payloads is treated defensively —
parsers tolerate missing keys and log nothing on absence. Validate field names
against live payloads during the smoke test and adjust here only.

Kalshi orderbook (GET /markets/{ticker}/orderbook) returns resting *bids* on each
side. The current production shape is fixed-point dollars::

    {"orderbook_fp": {"yes_dollars": [["0.5900", "254.00"], ...],
                       "no_dollars":  [["0.9820", "12.00"], ...]}}

i.e. ``yes`` = YES bids, ``no`` = NO bids, each ``[price_dollars_str, size_str]``
where prices go to deci-cent ($0.001) and sizes may be fractional. The ask ladders
are implied (a NO bid @ q == a YES ask @ 1-q) and derived later by
``hot_path.normalize``. A legacy integer-cents ``orderbook`` shape is also handled.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from stellasaurus.common.money import cents_to_micros, dollars_to_micros
from stellasaurus.common.types import Venue
from stellasaurus.hot_path.book import NativeBook, PriceLevel


def _to_epoch_ms(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        # Heuristic: seconds vs milliseconds.
        return int(value) if value > 1e11 else int(value * 1000)
    if isinstance(value, str):
        try:
            return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
        except ValueError:
            return None
    return None


def parse_market(d: dict[str, Any]) -> dict[str, Any]:
    """Extract the catalog fields we care about (returned as a plain dict so the
    caller can compute the fingerprint and build a RawMarket)."""
    return {
        "native_id": d.get("ticker") or d.get("market_ticker") or "",
        "title": d.get("title") or d.get("yes_sub_title") or d.get("subtitle") or "",
        "rules_text": d.get("rules_primary") or d.get("subtitle") or d.get("yes_sub_title"),
        "settlement_source": d.get("settlement_source") or d.get("source"),
        "resolves_at_ms": _to_epoch_ms(
            d.get("close_time") or d.get("expiration_time") or d.get("expected_expiration_time")
        ),
        "status": d.get("status"),
    }


def _levels_fp(raw: Any) -> tuple[PriceLevel, ...]:
    """Fixed-point dollars shape: [price_dollars_str, size_str] (size may be fractional)."""
    if not raw:
        return ()
    out: list[PriceLevel] = []
    for item in raw:
        try:
            price = dollars_to_micros(str(item[0]))
            size = int(round(float(item[1])))
        except (TypeError, ValueError, IndexError):
            continue
        if size > 0 and price > 0:
            out.append(PriceLevel(price=price, size=size))
    return tuple(out)


def _levels_cents(raw: Any) -> tuple[PriceLevel, ...]:
    """Legacy integer-cents shape: [price_cents, size]."""
    if not raw:
        return ()
    out: list[PriceLevel] = []
    for item in raw:
        try:
            price_cents, size = int(item[0]), int(item[1])
        except (TypeError, ValueError, IndexError):
            continue
        if size > 0:
            out.append(PriceLevel(price=cents_to_micros(price_cents), size=size))
    return tuple(out)


def parse_orderbook(
    *, ticker: str, payload: dict[str, Any], seq: int, recv_mono_ns: int, recv_wall_ms: int
) -> NativeBook:
    ob_fp = payload.get("orderbook_fp")
    if ob_fp is not None:
        yes_bids = _levels_fp(ob_fp.get("yes_dollars"))
        no_bids = _levels_fp(ob_fp.get("no_dollars"))
    else:
        ob = payload.get("orderbook", payload) or {}
        yes_bids = _levels_cents(ob.get("yes"))
        no_bids = _levels_cents(ob.get("no"))
    return NativeBook(
        venue=Venue.KALSHI,
        native_id=ticker,
        yes_bids=yes_bids,
        yes_asks=None,  # implied by NO bids; derived in normalize
        no_bids=no_bids,
        no_asks=None,  # implied by YES bids; derived in normalize
        seq=seq,
        recv_mono_ns=recv_mono_ns,
        recv_wall_ms=recv_wall_ms,
    )
