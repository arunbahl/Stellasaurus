"""Kalshi WebSocket market-data feed (orderbook snapshot + delta).

Requires an authenticated session, so it is used only when Kalshi credentials are
present; otherwise the composition root falls back to ``RestPollFeed``. Maintains
a working per-ticker book and emits a fresh ``NativeBook`` on every update.

NOTE: message shapes follow the documented ``orderbook_snapshot`` /
``orderbook_delta`` model and should be verified against live frames (recorded
into the replay fixtures) before relying on the WS path.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence

import websockets

from stellasaurus.common.clock import mono_ns, wall_ms
from stellasaurus.common.config import Settings
from stellasaurus.common.logging import get_logger
from stellasaurus.common.money import cents_to_micros
from stellasaurus.common.types import Venue
from stellasaurus.hot_path.book import NativeBook, PriceLevel
from stellasaurus.venues.base import FeedStats, OnBook
from stellasaurus.venues.signing import KalshiSigner

_log = get_logger("venues.kalshi.stream")


class KalshiStream:
    def __init__(self, settings: Settings, signer: KalshiSigner) -> None:
        self._url = settings.kalshi_ws_url
        self._signer = signer
        self.stats = FeedStats(venue=Venue.KALSHI, transport="WS")
        # working book: ticker -> side -> {price_micros: size}
        self._books: dict[str, dict[str, dict[int, int]]] = {}
        self._seq = 0

    async def run(self, native_ids: Sequence[str], on_book: OnBook) -> None:
        sign_path = "/trade-api/ws/v2"
        backoff = 1.0
        while True:
            try:
                headers = self._signer.headers(
                    timestamp_ms=wall_ms(), method="GET", path=sign_path
                )
                async with websockets.connect(self._url, additional_headers=headers) as ws:
                    self.stats.connected = True
                    backoff = 1.0
                    await ws.send(
                        json.dumps(
                            {
                                "id": 1,
                                "cmd": "subscribe",
                                "params": {
                                    "channels": ["orderbook_delta"],
                                    "market_tickers": list(native_ids),
                                },
                            }
                        )
                    )
                    async for raw in ws:
                        self._handle(raw, on_book)
            except Exception as exc:  # noqa: BLE001 - reconnect on any failure
                self.stats.connected = False
                self.stats.reconnects += 1
                _log.warning("ws_reconnect", error=str(exc), backoff_s=backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    def _handle(self, raw: str | bytes, on_book: OnBook) -> None:
        msg = json.loads(raw)
        msg_type = msg.get("type")
        data = msg.get("msg", msg)
        ticker = data.get("market_ticker")
        if not ticker:
            return
        if msg_type == "orderbook_snapshot":
            book = {"yes": {}, "no": {}}
            for side in ("yes", "no"):
                for price_cents, size in data.get(side, []) or []:
                    book[side][cents_to_micros(int(price_cents))] = int(size)
            self._books[ticker] = book
        elif msg_type == "orderbook_delta":
            book = self._books.setdefault(ticker, {"yes": {}, "no": {}})
            side = data.get("side")
            price = cents_to_micros(int(data["price"]))
            delta = int(data.get("delta", 0))
            new_size = book.get(side, {}).get(price, 0) + delta
            if new_size <= 0:
                book.setdefault(side, {}).pop(price, None)
            else:
                book.setdefault(side, {})[price] = new_size
        else:
            return
        self.stats.mark_frame()
        on_book(self._snapshot(ticker))

    def _snapshot(self, ticker: str) -> NativeBook:
        self._seq += 1
        book = self._books.get(ticker, {"yes": {}, "no": {}})
        yes_bids = tuple(PriceLevel(p, s) for p, s in book.get("yes", {}).items())
        no_bids = tuple(PriceLevel(p, s) for p, s in book.get("no", {}).items())
        return NativeBook(
            venue=Venue.KALSHI,
            native_id=ticker,
            yes_bids=yes_bids,
            yes_asks=None,
            no_bids=no_bids,
            no_asks=None,
            seq=self._seq,
            recv_mono_ns=mono_ns(),
            recv_wall_ms=wall_ms(),
        )
