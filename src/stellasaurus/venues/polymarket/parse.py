"""Parse Polymarket US JSON into venue-neutral DTOs.

NOTE: field availability is treated defensively; verify against live payloads
during the smoke test. The market book (GET /v1/markets/{slug}/book) returns
``bids`` and ``offers`` (asks) for the YES outcome, each level carrying ``px``
(price) and ``qty``. The NO side is implied and derived later in
``hot_path.normalize``.

Prices (``px``) are dollars in (0, 1). They are converted ONCE here to integer
micro-USD via ``dollars_to_micros`` (string-based, no float).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from stellasaurus.common.money import dollars_to_micros
from stellasaurus.common.types import Venue
from stellasaurus.hot_path.book import NativeBook, PriceLevel


def _to_epoch_ms(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value) if value > 1e11 else int(value * 1000)
    if isinstance(value, str):
        try:
            return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
        except ValueError:
            return None
    return None


def _amount_to_decimal(value: Any) -> Decimal | None:
    """Polymarket money may be a scalar or an {value, currency} Amount object."""
    if isinstance(value, dict):
        value = value.get("value")
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def parse_market(d: dict[str, Any]) -> dict[str, Any]:
    return {
        "native_id": d.get("slug") or d.get("marketSlug") or d.get("id") or "",
        "title": d.get("title") or d.get("question") or d.get("name") or "",
        "rules_text": d.get("description") or d.get("rules") or d.get("resolutionSource"),
        "settlement_source": d.get("resolutionSource") or d.get("settlementSource"),
        "resolves_at_ms": _to_epoch_ms(
            d.get("endDate") or d.get("resolveTime") or d.get("closeTime")
        ),
        "status": d.get("status") or d.get("state"),
    }


def _levels(raw: Any) -> tuple[PriceLevel, ...]:
    if not raw:
        return ()
    out: list[PriceLevel] = []
    for item in raw:
        px = _amount_to_decimal(item.get("px") if isinstance(item, dict) else item[0])
        qty_raw = item.get("qty") if isinstance(item, dict) else item[1]
        if px is None or qty_raw is None:
            continue
        try:
            qty = int(Decimal(str(qty_raw)))
        except (InvalidOperation, ValueError):
            continue
        if qty > 0 and px > 0:
            out.append(PriceLevel(price=dollars_to_micros(px), size=qty))
    return tuple(out)


def parse_book(
    *, slug: str, payload: dict[str, Any], seq: int, recv_mono_ns: int, recv_wall_ms: int
) -> NativeBook:
    book = payload.get("book", payload) or {}
    yes_bids = _levels(book.get("bids"))
    yes_asks = _levels(book.get("offers") or book.get("asks"))
    return NativeBook(
        venue=Venue.POLYMARKET,
        native_id=slug,
        yes_bids=yes_bids,
        yes_asks=yes_asks,
        no_bids=None,  # implied; derived in normalize
        no_asks=None,
        seq=seq,
        recv_mono_ns=recv_mono_ns,
        recv_wall_ms=recv_wall_ms,
    )
