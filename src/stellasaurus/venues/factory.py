"""Build venue clients and feeds, choosing transport by credential availability.

Keyless (Phase 1 default) -> REST-poll feed on public market data.
Credentialed                -> authenticated WebSocket feed.
"""

from __future__ import annotations

import httpx

from stellasaurus.common.config import Settings
from stellasaurus.common.logging import get_logger
from stellasaurus.common.types import Venue
from stellasaurus.venues.base import MarketFeed, RestPollFeed, VenueClient
from stellasaurus.venues.kalshi.client import KalshiClient
from stellasaurus.venues.kalshi.stream import KalshiStream
from stellasaurus.venues.polymarket.client import PolymarketClient
from stellasaurus.venues.polymarket.stream import PolymarketStream
from stellasaurus.venues.signing import KalshiSigner, PolymarketSigner

_log = get_logger("venues.factory")


def build_kalshi(settings: Settings, http: httpx.AsyncClient) -> tuple[VenueClient, MarketFeed]:
    client = KalshiClient(settings, http)
    if settings.kalshi_credentials_present and settings.kalshi_private_key_path is not None:
        signer = KalshiSigner(settings.kalshi_api_key_id or "", settings.kalshi_private_key_path)
        _log.info("kalshi_transport", transport="WS")
        return client, KalshiStream(settings, signer)
    _log.info("kalshi_transport", transport="REST_POLL", reason="no_credentials")
    return client, RestPollFeed(client=client, interval_ms=settings.rest_poll_interval_ms)


def build_polymarket(settings: Settings, http: httpx.AsyncClient) -> tuple[VenueClient, MarketFeed]:
    client = PolymarketClient(settings, http)
    if settings.poly_credentials_present:
        signer = PolymarketSigner(settings.poly_access_key or "", settings.poly_ed25519_seed or "")
        _log.info("poly_transport", transport="WS")
        return client, PolymarketStream(settings, signer)
    _log.info("poly_transport", transport="REST_POLL", reason="no_credentials")
    return client, RestPollFeed(client=client, interval_ms=settings.rest_poll_interval_ms)


def venue_clients(
    settings: Settings, http: httpx.AsyncClient
) -> dict[Venue, VenueClient]:
    return {
        Venue.KALSHI: KalshiClient(settings, http),
        Venue.POLYMARKET: PolymarketClient(settings, http),
    }
