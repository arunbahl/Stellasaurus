"""Kalshi REST client (catalog + book snapshots).

Public market-data reads need no signature; signing is wired (via ``KalshiSigner``)
for authenticated endpoints used in later phases. Kept thin so it can be swapped
for an OpenAPI-generated client or ``kalshi-sdk`` without touching callers.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from stellasaurus.common.clock import mono_ns, wall_ms
from stellasaurus.common.config import Settings
from stellasaurus.common.logging import get_logger
from stellasaurus.common.types import Venue
from stellasaurus.hot_path.book import NativeBook
from stellasaurus.venues.base import RawMarket, VenueClient
from stellasaurus.venues.kalshi import parse
from stellasaurus.venues.signing import KalshiSigner

_log = get_logger("venues.kalshi.client")


class KalshiClient(VenueClient):
    venue = Venue.KALSHI

    def __init__(self, settings: Settings, http: httpx.AsyncClient) -> None:
        self._base = settings.kalshi_rest_base.rstrip("/")
        self._http = http
        self._page_size = settings.kalshi_catalog_page_size
        self._max_pages = settings.kalshi_catalog_max_pages
        self._page_pause_s = settings.kalshi_catalog_page_pause_ms / 1000.0
        self._signer: KalshiSigner | None = None
        if settings.kalshi_credentials_present:
            assert settings.kalshi_private_key_path is not None
            self._signer = KalshiSigner(
                settings.kalshi_api_key_id or "", settings.kalshi_private_key_path
            )

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        headers: dict[str, str] = {}
        if self._signer is not None:
            # Sign the API path (without host); trade-api/v2 is part of the path.
            sign_path = path if path.startswith("/trade-api") else f"/trade-api/v2{path}"
            headers = self._signer.headers(timestamp_ms=wall_ms(), method="GET", path=sign_path)
        resp = await self._http.get(f"{self._base}{path}", params=params, headers=headers)
        resp.raise_for_status()
        return resp.json()

    async def list_markets(self) -> list[RawMarket]:
        markets: list[RawMarket] = []
        cursor: str | None = None
        # Gentle pagination: Kalshi rate-limits aggressively. Cap pages, pace
        # requests, and stop early on 429 rather than hammering (returning the
        # partial catalog we have so far).
        for page in range(self._max_pages):
            params = {"limit": self._page_size, "status": "open"}
            if cursor:
                params["cursor"] = cursor
            try:
                data = await self._get("/markets", params=params)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 429:
                    _log.warning("rate_limited", fetched=len(markets), page=page)
                    break
                raise
            for raw in data.get("markets", []):
                fields = parse.parse_market(raw)
                if not fields["native_id"]:
                    continue
                markets.append(
                    RawMarket(
                        venue=Venue.KALSHI,
                        native_id=fields["native_id"],
                        title=fields["title"],
                        rules_text=fields["rules_text"],
                        settlement_source=fields["settlement_source"],
                        resolves_at_ms=fields["resolves_at_ms"],
                        status=fields["status"],
                        raw=raw,
                    )
                )
            cursor = data.get("cursor") or None
            if not cursor:
                break
            await asyncio.sleep(self._page_pause_s)
        _log.info("listed_markets", count=len(markets))
        return markets

    async def get_market(self, native_id: str) -> RawMarket | None:
        try:
            data = await self._get(f"/markets/{native_id}")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise
        raw = data.get("market", data)
        fields = parse.parse_market(raw)
        if not fields["native_id"]:
            return None
        return RawMarket(
            venue=Venue.KALSHI,
            native_id=fields["native_id"],
            title=fields["title"],
            rules_text=fields["rules_text"],
            settlement_source=fields["settlement_source"],
            resolves_at_ms=fields["resolves_at_ms"],
            status=fields["status"],
            raw=raw,
        )

    async def get_book(self, native_id: str) -> NativeBook | None:
        data = await self._get(f"/markets/{native_id}/orderbook")
        return parse.parse_orderbook(
            ticker=native_id,
            payload=data,
            seq=0,
            recv_mono_ns=mono_ns(),
            recv_wall_ms=wall_ms(),
        )
