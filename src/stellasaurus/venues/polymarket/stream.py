"""Polymarket US WebSocket market-data feed.

The WS handshake requires Ed25519 auth, so this is used only when Polymarket
credentials are present; otherwise the composition root falls back to
``RestPollFeed``. Treats each market-data message as the current book for its
slug (replace-on-update).

NOTE: subscription and message shapes follow the documented ``markets`` channel
and should be verified against live frames before relying on the WS path.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence

import websockets

from stellasaurus.common.clock import mono_ns, wall_ms
from stellasaurus.common.config import Settings
from stellasaurus.common.logging import get_logger
from stellasaurus.common.types import Venue
from stellasaurus.venues.base import FeedStats, OnBook
from stellasaurus.venues.polymarket import parse
from stellasaurus.venues.signing import PolymarketSigner

_log = get_logger("venues.polymarket.stream")


class PolymarketStream:
    def __init__(self, settings: Settings, signer: PolymarketSigner) -> None:
        self._url = settings.poly_ws_url
        self._signer = signer
        self.stats = FeedStats(venue=Venue.POLYMARKET, transport="WS")
        self._seq = 0

    async def run(self, native_ids: Sequence[str], on_book: OnBook) -> None:
        backoff = 1.0
        while True:
            try:
                # Sign the WS path for the handshake.
                headers = self._signer.headers(
                    timestamp_ms=wall_ms(), method="GET", path="/v1/ws/markets"
                )
                async with websockets.connect(self._url, additional_headers=headers) as ws:
                    self.stats.connected = True
                    backoff = 1.0
                    await ws.send(
                        json.dumps(
                            {
                                "subscribe": {
                                    "requestId": "stella-1",
                                    "subscriptionType": "SUBSCRIPTION_TYPE_MARKET_DATA",
                                    "marketSlugs": list(native_ids),
                                }
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
        data = msg.get("marketData") or msg.get("data") or msg
        slug = data.get("marketSlug") or data.get("slug")
        if not slug or not (data.get("bids") or data.get("offers") or data.get("asks")):
            return
        self._seq += 1
        book = parse.parse_book(
            slug=slug,
            payload=data,
            seq=self._seq,
            recv_mono_ns=mono_ns(),
            recv_wall_ms=wall_ms(),
        )
        self.stats.mark_frame()
        on_book(book)
