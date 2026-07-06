"""Real order gateways (Phase 6) — HARD-GATED behind ``live_trading_enabled``.

⚠️  UNVALIDATED: request/response field names follow each venue's documentation
and prior research but have NOT been exercised against a demo/sandbox or live
account. Before any live use: run Kalshi's demo environment and a minimal
Polymarket order, and reconcile the response shapes. Every submit is refused
unless ``live_trading_enabled`` is true — and the composition root additionally
never wires these unless that flag is set, so the gate is double-layered.

Both gateways implement ``OrderGateway``: place a single FOK limit BUY for a
canonical side, returning a normalized ``OrderResult``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from stellasaurus.common.clock import wall_ms
from stellasaurus.common.config import Settings
from stellasaurus.common.logging import get_logger
from stellasaurus.common.types import Micros, OutcomePolarity, Side, Venue
from stellasaurus.venues.signing import KalshiSigner, PolymarketSigner

_log = get_logger("venues.orders")


@dataclass(frozen=True, slots=True)
class OrderResult:
    venue: Venue
    native_id: str
    side: Side
    requested_qty: int
    filled_qty: int
    avg_price_micros: Micros | None
    fees_micros: Micros | None  # venue-reported when available
    order_id: str | None
    raw: dict[str, Any]

    @property
    def fully_filled(self) -> bool:
        return self.filled_qty >= self.requested_qty


class OrderGateway(Protocol):
    venue: Venue

    async def buy_fok(
        self, *, native_id: str, side: Side, qty: int, limit_price_micros: Micros,
        polarity: OutcomePolarity,
    ) -> OrderResult: ...


class LiveGateDisabledError(RuntimeError):
    pass


class KalshiOrderGateway:
    """POST /portfolio/orders — RSA-signed. Prices are integer cents."""

    venue = Venue.KALSHI

    def __init__(self, settings: Settings, http: httpx.AsyncClient) -> None:
        if not settings.kalshi_credentials_present:
            raise ValueError("Kalshi credentials required for the order gateway")
        assert settings.kalshi_private_key_path is not None
        self._signer = KalshiSigner(
            settings.kalshi_api_key_id or "", settings.kalshi_private_key_path
        )
        self._base = settings.kalshi_rest_base.rstrip("/")
        self._http = http
        self._enabled = settings.live_trading_enabled

    async def buy_fok(
        self, *, native_id: str, side: Side, qty: int, limit_price_micros: Micros,
        polarity: OutcomePolarity,
    ) -> OrderResult:
        if not self._enabled:
            raise LiveGateDisabledError("live_trading_enabled is false")
        # Kalshi orders are in NATIVE side terms; canonical side maps through
        # polarity (Kalshi is always the canonical reference in our pairs).
        native_side = side.value.lower()
        price_cents = max(1, min(99, round(limit_price_micros / 10_000)))
        body = {
            "ticker": native_id,
            "client_order_id": f"stella-{uuid.uuid4().hex[:16]}",
            "action": "buy",
            "side": native_side,
            "count": qty,
            "type": "limit",
            f"{native_side}_price": price_cents,
            "time_in_force": "fill_or_kill",  # VERIFY against demo before live
        }
        path = "/trade-api/v2/portfolio/orders"
        headers = self._signer.headers(timestamp_ms=wall_ms(), method="POST", path=path)
        r = await self._http.post(f"{self._base}/portfolio/orders", json=body, headers=headers)
        r.raise_for_status()
        raw = r.json().get("order", r.json())
        filled = int(raw.get("filled_count") or raw.get("count_filled") or 0)
        return OrderResult(
            venue=self.venue, native_id=native_id, side=side,
            requested_qty=qty, filled_qty=filled,
            avg_price_micros=None,  # reconcile from fills feed
            fees_micros=_dollars_to_micros_safe(raw.get("taker_fees_dollars")),
            order_id=raw.get("order_id"), raw=raw,
        )


class PolymarketOrderGateway:
    """POST /v1/orders — Ed25519-signed. Intents map canonical side via polarity."""

    venue = Venue.POLYMARKET

    def __init__(self, settings: Settings, http: httpx.AsyncClient) -> None:
        if not settings.poly_credentials_present:
            raise ValueError("Polymarket credentials required for the order gateway")
        self._signer = PolymarketSigner(
            settings.poly_access_key or "", settings.poly_ed25519_seed or ""
        )
        self._base = settings.poly_rest_base.rstrip("/")
        self._http = http
        self._enabled = settings.live_trading_enabled

    async def buy_fok(
        self, *, native_id: str, side: Side, qty: int, limit_price_micros: Micros,
        polarity: OutcomePolarity,
    ) -> OrderResult:
        if not self._enabled:
            raise LiveGateDisabledError("live_trading_enabled is false")
        # Canonical side -> native intent through the pair's polarity:
        # DIRECT: canonical YES == native long. INVERTED: canonical YES == native short.
        canonical_is_yes = side is Side.YES
        native_long = (
            canonical_is_yes if polarity is OutcomePolarity.DIRECT else not canonical_is_yes
        )
        intent = "ORDER_INTENT_BUY_LONG" if native_long else "ORDER_INTENT_BUY_SHORT"
        # A BUY_SHORT limit price is quoted in SHORT terms (1 - long price).
        price = limit_price_micros if native_long else 1_000_000 - limit_price_micros
        body = {
            "marketSlug": native_id,
            "clientOrderId": f"stella-{uuid.uuid4().hex[:16]}",
            "orderIntent": intent,
            "type": "ORDER_TYPE_LIMIT",
            "timeInForce": "TIME_IN_FORCE_FILL_OR_KILL",  # VERIFY before live
            "quantity": str(qty),
            "limitPrice": {"value": f"{price / 1_000_000:.4f}", "currency": "USD"},
        }
        path = "/v1/orders"
        headers = self._signer.headers(timestamp_ms=wall_ms(), method="POST", path=path)
        r = await self._http.post(f"{self._base}{path}", json=body, headers=headers)
        r.raise_for_status()
        raw = r.json().get("order", r.json())
        filled = int(float(raw.get("cumQuantity") or 0))
        avg = raw.get("avgPx", {})
        avg_micros = _dollars_to_micros_safe(avg.get("value") if isinstance(avg, dict) else avg)
        fees = raw.get("commissionNotionalTotalCollected", {})
        return OrderResult(
            venue=self.venue, native_id=native_id, side=side,
            requested_qty=qty, filled_qty=filled,
            avg_price_micros=avg_micros,
            fees_micros=_dollars_to_micros_safe(
                fees.get("value") if isinstance(fees, dict) else fees
            ),
            order_id=raw.get("id") or raw.get("orderId"), raw=raw,
        )


def _dollars_to_micros_safe(value: Any) -> Micros | None:
    if value is None:
        return None
    try:
        return int(round(float(value) * 1_000_000))
    except (TypeError, ValueError):
        return None
