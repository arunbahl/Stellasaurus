"""Live smoke tests — hit real public venue endpoints. Deselected by default.

Run explicitly with:  pytest -m live

These confirm the parsers match live payload shapes (the biggest Phase-1 unknown,
DESIGN risks #1/#2). They are tolerant: a venue requiring auth even for public
reads, or a network block, skips rather than fails — but a 200 with an
unparseable shape DOES fail, which is the signal we want.
"""

import httpx
import pytest

from stellasaurus.common.config import load_settings
from stellasaurus.venues.kalshi.client import KalshiClient
from stellasaurus.venues.polymarket.client import PolymarketClient

pytestmark = pytest.mark.live


@pytest.fixture
def settings():
    return load_settings()


@pytest.mark.asyncio
async def test_kalshi_public_catalog(settings):
    async with httpx.AsyncClient(timeout=10.0) as http:
        client = KalshiClient(settings, http)
        try:
            markets = await client.list_markets()
        except httpx.HTTPError as exc:
            pytest.skip(f"Kalshi unreachable/needs auth: {exc}")
        assert isinstance(markets, list)
        if markets:
            assert markets[0].native_id  # parser produced a usable ticker


@pytest.mark.asyncio
async def test_polymarket_public_catalog(settings):
    async with httpx.AsyncClient(timeout=10.0) as http:
        client = PolymarketClient(settings, http)
        try:
            markets = await client.list_markets()
        except httpx.HTTPError as exc:
            pytest.skip(f"Polymarket unreachable/needs auth: {exc}")
        assert isinstance(markets, list)
        if markets:
            assert markets[0].native_id
