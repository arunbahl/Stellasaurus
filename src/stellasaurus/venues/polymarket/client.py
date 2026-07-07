"""Polymarket US REST client (catalog + book snapshots).

Public market-data endpoints need no auth; the Ed25519 signer is wired for the
authenticated endpoints used later. Kept thin so it can be swapped for the
official ``polymarket-us`` SDK without touching callers.
"""

from __future__ import annotations

from typing import Any

import httpx

from stellasaurus.common.clock import mono_ns, wall_ms
from stellasaurus.common.config import Settings
from stellasaurus.common.logging import get_logger
from stellasaurus.common.types import Venue
from stellasaurus.hot_path.book import NativeBook
from stellasaurus.venues.base import RawMarket, VenueClient
from stellasaurus.venues.polymarket import parse
from stellasaurus.venues.signing import PolymarketSigner

_log = get_logger("venues.polymarket.client")


class PolymarketClient(VenueClient):
    venue = Venue.POLYMARKET

    def __init__(self, settings: Settings, http: httpx.AsyncClient) -> None:
        self._base = settings.poly_rest_base.rstrip("/")
        self._http = http
        self._signer: PolymarketSigner | None = None
        if settings.poly_credentials_present:
            self._signer = PolymarketSigner(
                settings.poly_access_key or "", settings.poly_ed25519_seed or ""
            )

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        headers: dict[str, str] = {}
        if self._signer is not None:
            headers = self._signer.headers(timestamp_ms=wall_ms(), method="GET", path=path)
        resp = await self._http.get(f"{self._base}{path}", params=params, headers=headers)
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        return data

    async def list_markets(self) -> list[RawMarket]:
        # Pagination is OFFSET-based (a `cursor` param is silently ignored, and the
        # default sort returns the oldest markets first — which is how the frozen
        # 2025 seed markets masked the live catalog). Filter to open markets.
        markets: list[RawMarket] = []
        page_size = 500
        for page in range(20):  # safety bound
            data = await self._get(
                "/v1/markets",
                params={
                    "limit": page_size,
                    "offset": page * page_size,
                    "active": "true",
                    "closed": "false",
                },
            )
            items = data.get("markets", data if isinstance(data, list) else [])
            for raw in items:
                fields = parse.parse_market(raw)
                if not fields["native_id"]:
                    continue
                markets.append(
                    RawMarket(
                        venue=Venue.POLYMARKET,
                        native_id=fields["native_id"],
                        title=fields["title"],
                        rules_text=fields["rules_text"],
                        settlement_source=fields["settlement_source"],
                        resolves_at_ms=fields["resolves_at_ms"],
                        status=fields["status"],
                        raw=raw,
                    )
                )
            if len(items) < page_size:
                break
        _log.info("listed_markets", count=len(markets))
        return markets

    async def get_market(self, native_id: str) -> RawMarket | None:
        try:
            data = await self._get(f"/v1/market/slug/{native_id}")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise
        raw = data.get("market", data)
        fields = parse.parse_market(raw)
        if not fields["native_id"]:
            return None
        return RawMarket(
            venue=Venue.POLYMARKET,
            native_id=fields["native_id"],
            title=fields["title"],
            rules_text=fields["rules_text"],
            settlement_source=fields["settlement_source"],
            resolves_at_ms=fields["resolves_at_ms"],
            status=fields["status"],
            raw=raw,
        )

    async def get_book(self, native_id: str) -> NativeBook | None:
        data = await self._get(f"/v1/markets/{native_id}/book")
        return parse.parse_book(
            slug=native_id,
            payload=data,
            seq=0,
            recv_mono_ns=mono_ns(),
            recv_wall_ms=wall_ms(),
        )
