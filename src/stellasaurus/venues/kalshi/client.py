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
        self._series_per_cycle = settings.kalshi_series_per_cycle
        self._series_rotation: list[str] = []  # full series list, swept in chunks
        self._series_offset = 0
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

    def _to_raw(self, raw: dict[str, Any]) -> RawMarket | None:
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

    async def list_series_tickers(self) -> list[str]:
        """ALL series tickers — no category filtering; every series is eligible.

        The only exclusion is structural: multivariate parlay collections
        (KXMVE*) cannot form a clean two-leg locked pair regardless of topic.
        """
        tickers: list[str] = []
        cursor: str | None = None
        for _ in range(30):
            params: dict[str, Any] = {"limit": 1000}
            if cursor:
                params["cursor"] = cursor
            data = await self._get("/series", params=params)
            batch = data.get("series", [])
            for x in batch:
                t = x.get("ticker") or ""
                if t and not t.upper().startswith("KXMVE"):
                    tickers.append(t)
            cursor = data.get("cursor") or None
            if not cursor or not batch:
                break
            await asyncio.sleep(self._page_pause_s)
        _log.info("listed_series", count=len(tickers))
        return tickers

    async def list_markets(self) -> list[RawMarket]:
        """Sweep the next chunk of the full series rotation and return their open
        markets. Successive calls advance the rotation so ALL ~11k series are
        visited over a few cycles — the accumulated catalog lives in the markets
        table (catalog sync upserts each sweep's results).

        Rationale: the global /markets list is ~99.6% KXMVE parlay markets, so
        exhaustive per-series enumeration is the only way to see the real
        catalog without semantic pre-filtering.
        """
        if not self._series_rotation or self._series_offset >= len(self._series_rotation):
            self._series_rotation = await self.list_series_tickers()
            self._series_offset = 0
        chunk = self._series_rotation[
            self._series_offset : self._series_offset + self._series_per_cycle
        ]
        self._series_offset += len(chunk)

        markets: list[RawMarket] = []
        for st in chunk:
            try:
                data = await self._get(
                    "/markets",
                    params={"limit": 200, "status": "open", "series_ticker": st},
                )
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 429:
                    _log.warning("rate_limited", fetched=len(markets), series=st)
                    # rewind so the unswept remainder is retried next cycle
                    self._series_offset -= len(chunk) - chunk.index(st)
                    break
                continue
            for raw in data.get("markets", []):
                m = self._to_raw(raw)
                if m is not None:
                    markets.append(m)
            await asyncio.sleep(self._page_pause_s)
        _log.info(
            "listed_markets",
            count=len(markets),
            swept_series=len(chunk),
            rotation=f"{self._series_offset}/{len(self._series_rotation)}",
        )
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
