"""Real order gateways (Phase 6) — HARD-GATED behind ``live_trading_enabled``.

✅ SHAPES VALIDATED LIVE (2026-07-06, Stage-1 unmarketable-FOK probes on both
production venues, zero fills, zero cost):
  * Kalshi V2: POST /portfolio/events/orders, single-book bid/ask semantics,
    fixed-point string count/price; unmarketable FOK -> 409
    fill_or_kill_insufficient_resting_volume (treated as clean zero-fill).
  * Polymarket: POST /v1/orders with intent/tif/price(Amount)/quantity; cancel
    is POST /v1/order/{id}/cancel. TWO validated footguns:
      (1) timeInForce/limitPrice field names are silently IGNORED (order rests
          as DAY) — use tif/price.
      (2) the create response's `executions` array is EMPTY even on a FULL FILL.
          Fill detection MUST read `cumQuantity` from an order lookup, never the
          create response — see `_settle`. (Trusting `executions` reported real
          fills as misses and left naked positions.)
    Poly exposes a single YES book; canonical-NO is bought via
    ORDER_INTENT_BUY_SHORT (validated fills register as YES-sold).

Every submit is refused unless ``live_trading_enabled`` is true — and the
composition root additionally never wires these unless that flag is set, so the
gate is double-layered. Both gateways implement ``OrderGateway``: place a
single FOK limit BUY for a canonical side, returning a normalized
``OrderResult``. Fill-path fields (executions parsing, average_fill_price)
remain to be exercised by the first marketable order (Stage 2).
"""

from __future__ import annotations

import asyncio
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

# Poll budget for Polymarket fill settlement (read-your-write lag). A FOK is
# terminal server-side fast; ~1.5s covers propagation without stalling misses.
_POLL_ATTEMPTS = 10
_POLL_INTERVAL_S = 0.15

_CENT = 10_000  # micros


def _floor_cent(micros: Micros) -> Micros:
    """Round DOWN to the venue's cent tick — for BUY limits (never pay above
    intent; validated: off-tick prices are rejected with invalid_price)."""
    return max(_CENT, (micros // _CENT) * _CENT)


def _ceil_cent(micros: Micros) -> Micros:
    """Round UP to the cent tick — for ASK prices derived from a NO limit
    (selling YES no lower than intended keeps the NO cost within intent)."""
    return -(-micros // _CENT) * _CENT


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
    """Kalshi V2 orders — RSA-signed, single-book bid/ask, fixed-point strings."""

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
        """V2 single-book semantics (validated live 2026-07-06): ``side`` is
        bid/ask on the YES leg. Buying canonical YES = bid at p. Buying
        canonical NO at q = ASK on YES at (1 - q) — selling YES you don't hold
        mints the NO position at cost q on a crossed event-contract book."""
        if not self._enabled:
            raise LiveGateDisabledError("live_trading_enabled is false")
        if side is Side.YES:
            book_side, price_micros = "bid", _floor_cent(limit_price_micros)
        else:
            book_side, price_micros = "ask", _ceil_cent(1_000_000 - limit_price_micros)
        body = {
            "ticker": native_id,
            "client_order_id": f"stella-{uuid.uuid4().hex[:16]}",
            "side": book_side,
            "count": f"{qty}.00",
            "price": f"{price_micros / 1_000_000:.4f}",
            "time_in_force": "fill_or_kill",
            "self_trade_prevention_type": "taker_at_cross",
        }
        path = "/trade-api/v2/portfolio/events/orders"
        headers = self._signer.headers(timestamp_ms=wall_ms(), method="POST", path=path)
        r = await self._http.post(
            f"{self._base}/portfolio/events/orders", json=body, headers=headers
        )
        if r.status_code == 409 and "fill_or_kill" in r.text:
            # Validated live: unmarketable FOK -> 409 insufficient_resting_volume.
            # That is a clean zero-fill, not a transport error.
            return OrderResult(
                venue=self.venue, native_id=native_id, side=side,
                requested_qty=qty, filled_qty=0, avg_price_micros=None,
                fees_micros=None, order_id=None, raw=r.json(),
            )
        r.raise_for_status()
        raw = r.json().get("order", r.json())
        filled = int(float(raw.get("fill_count") or 0))
        avg_yes = _dollars_to_micros_safe(raw.get("average_fill_price"))
        # ask fills report the YES sale price; the NO cost is its complement.
        avg = avg_yes if side is Side.YES or avg_yes is None else 1_000_000 - avg_yes
        avg_fee = _dollars_to_micros_safe(raw.get("average_fee_paid"))
        return OrderResult(
            venue=self.venue, native_id=native_id, side=side,
            requested_qty=qty, filled_qty=filled,
            avg_price_micros=avg,
            fees_micros=(avg_fee * filled) if (avg_fee is not None and filled) else None,
            order_id=raw.get("order_id"), raw=raw,
        )

    async def net_position(self, native_id: str) -> int:
        """Authoritative signed position from the venue: +N long YES, -N net NO.
        The source of truth for flattening — never trust our own record."""
        path = "/trade-api/v2/portfolio/positions"
        h = self._signer.headers(timestamp_ms=wall_ms(), method="GET", path=path)
        r = await self._http.get(f"{self._base}/portfolio/positions", headers=h)
        r.raise_for_status()
        for mp in r.json().get("market_positions", []):
            if mp.get("ticker") == native_id:
                return int(round(float(mp.get("position", 0))))
        return 0

    async def close_position(self, native_id: str) -> int:
        """Reduce-only marketable IOC to flatten. Returns the residual implied by
        the order's OWN fill (net - filled), NOT a portfolio re-read: position
        reads lag writes, and a stale "still open" read would make us sell again
        and open a naked short. reduce_only additionally guarantees an order can
        never OPEN exposure on the venue side."""
        if not self._enabled:
            raise LiveGateDisabledError("live_trading_enabled is false")
        net = await self.net_position(native_id)
        if net == 0:
            return 0
        book_side = "ask" if net > 0 else "bid"
        price = "0.0100" if net > 0 else "0.9900"  # cross to the far touch
        body = {
            "ticker": native_id, "client_order_id": f"flat-{uuid.uuid4().hex[:12]}",
            "side": book_side, "count": f"{abs(net)}.00", "price": price,
            "time_in_force": "immediate_or_cancel",
            "self_trade_prevention_type": "taker_at_cross", "reduce_only": True,
        }
        path = "/trade-api/v2/portfolio/events/orders"
        h = self._signer.headers(timestamp_ms=wall_ms(), method="POST", path=path)
        r = await self._http.post(
            f"{self._base}/portfolio/events/orders", json=body, headers=h
        )
        # 409 insufficient volume == nothing crossed; full position still stands.
        if r.status_code == 409:
            return net
        r.raise_for_status()
        filled = int(float(r.json().get("order", r.json()).get("fill_count") or 0))
        return net - filled if net > 0 else net + filled


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
        # VALIDATED (probe 2026-07-06): BUY_SHORT prices are quoted in SHORT
        # terms, and the canonical price of the side being bought IS the native
        # price of the mapped intent (both polarities) — no conversion, ever.
        # (A 1-limit conversion here silently destroys the slippage cap.)
        price = _floor_cent(limit_price_micros)
        # Field names VALIDATED live 2026-07-06: intent, tif, price (Amount),
        # quantity string. timeInForce/limitPrice are silently IGNORED (an
        # order defaults to DAY and rests) — never reintroduce them.
        body = {
            "marketSlug": native_id,
            "clientOrderId": f"stella-{uuid.uuid4().hex[:16]}",
            "intent": intent,
            "type": "ORDER_TYPE_LIMIT",
            "tif": "TIME_IN_FORCE_FILL_OR_KILL",
            "quantity": str(qty),
            "price": {"value": f"{price / 1_000_000:.4f}", "currency": "USD"},
        }
        path = "/v1/orders"
        headers = self._signer.headers(timestamp_ms=wall_ms(), method="POST", path=path)
        r = await self._http.post(f"{self._base}{path}", json=body, headers=headers)
        r.raise_for_status()
        raw = r.json()
        order_id = raw.get("id")

        # CRITICAL (validated live 2026-07-06): the create-order response's
        # `executions` array is EMPTY even when the order FULLY FILLS. Trusting
        # it silently reported real fills as misses, unwound the other leg, and
        # left naked positions. The ONLY authoritative fill count is
        # `cumQuantity` from an order lookup — always poll it.
        filled_int, avg_micros, fees_micros = await self._settle(order_id, raw)
        return OrderResult(
            venue=self.venue, native_id=native_id, side=side,
            requested_qty=qty, filled_qty=filled_int,
            avg_price_micros=avg_micros, fees_micros=fees_micros,
            order_id=order_id, raw=raw,
        )

    async def _settle(
        self, order_id: str | None, create_raw: dict[str, Any]
    ) -> tuple[int, Micros | None, Micros | None]:
        """Authoritative fill via order lookup. Returns (filled_qty,
        avg_price_micros, fees_micros).

        Two validated realities force a POLL here, not a single read:
          * the create response's `executions` is empty even on a full fill;
          * the order lookup has READ-YOUR-WRITE LAG — an immediate GET can still
            show cumQuantity 0 for an order that filled (a single read unwound
            the wrong leg live on 2026-07-06).
        A FOK is terminal server-side almost immediately, so we poll the order
        until it reports terminal (leavesQuantity 0) or the budget expires."""
        def parse_execs(execs: list[dict[str, Any]]) -> tuple[float, float, float]:
            f = n = c = 0.0
            for ex in execs:
                q = float(ex.get("quantity") or ex.get("qty") or 0)
                px = ex.get("price") or ex.get("px") or {}
                pxv = float(px.get("value") if isinstance(px, dict) else px or 0)
                com = ex.get("commission") or ex.get("commissionNotional") or {}
                comv = float(com.get("value") if isinstance(com, dict) else com or 0)
                f += q
                n += q * pxv
                c += comv
            return f, n, c

        filled, notional, fees = parse_execs(create_raw.get("executions") or [])
        order: dict[str, Any] = {}
        if order_id is not None:
            lp = f"/v1/order/{order_id}"
            for _ in range(_POLL_ATTEMPTS):
                try:
                    h = self._signer.headers(timestamp_ms=wall_ms(), method="GET", path=lp)
                    rr = await self._http.get(f"{self._base}{lp}", headers=h)
                    if rr.status_code == 200:
                        order = rr.json().get("order", {})
                        leaves = order.get("leavesQuantity")
                        # terminal: nothing left working -> cumQuantity is final
                        if leaves is not None and float(leaves) == 0:
                            break
                except Exception as exc:  # noqa: BLE001
                    _log.warning("poly_order_lookup_failed", order_id=order_id, error=str(exc))
                await asyncio.sleep(_POLL_INTERVAL_S)
        cum = order.get("cumQuantity")
        if cum is not None:
            filled = float(cum)
            if not notional:  # no execution detail -> use the order's avg price
                avg = order.get("avgPx") or order.get("averagePrice") or order.get("price")
                avgv = float(avg.get("value") if isinstance(avg, dict) else avg or 0)
                notional = filled * avgv
        avg_micros = int(round(notional / filled * 1_000_000)) if filled else None
        fees_micros = int(round(fees * 1_000_000)) if fees else None
        return int(filled), avg_micros, fees_micros

    async def cancel(self, *, order_id: str, native_id: str) -> bool:
        """POST /v1/order/{id}/cancel (validated live). True on 200."""
        path = f"/v1/order/{order_id}/cancel"
        headers = self._signer.headers(timestamp_ms=wall_ms(), method="POST", path=path)
        r = await self._http.post(
            f"{self._base}{path}", json={"marketSlug": native_id}, headers=headers
        )
        return r.status_code == 200

    async def net_position(self, native_id: str) -> int:
        """Authoritative signed position: +N long YES, -N net NO (short)."""
        path = "/v1/portfolio/positions"
        headers = self._signer.headers(timestamp_ms=wall_ms(), method="GET", path=path)
        r = await self._http.get(f"{self._base}{path}", headers=headers)
        r.raise_for_status()
        p = r.json().get("positions", {}).get(native_id)
        return int(round(float(p.get("netPosition", 0)))) if p else 0

    async def close_position(self, native_id: str) -> int:
        """Marketable IOC to flatten. Long YES -> SELL_LONG@0.01; net NO ->
        BUY_LONG@0.99 to cover. Residual is derived from the close order's OWN
        fill (polled via _settle), NOT a portfolio re-read — position reads lag
        writes, and a stale read would make us sell again into a naked short."""
        if not self._enabled:
            raise LiveGateDisabledError("live_trading_enabled is false")
        net = await self.net_position(native_id)
        if net == 0:
            return 0
        intent = "ORDER_INTENT_SELL_LONG" if net > 0 else "ORDER_INTENT_BUY_LONG"
        price = "0.0100" if net > 0 else "0.9900"  # cross to the far touch
        body = {
            "marketSlug": native_id, "clientOrderId": f"flat-{uuid.uuid4().hex[:12]}",
            "intent": intent, "type": "ORDER_TYPE_LIMIT",
            "tif": "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL",
            "quantity": str(abs(net)), "price": {"value": price, "currency": "USD"},
        }
        path = "/v1/orders"
        headers = self._signer.headers(timestamp_ms=wall_ms(), method="POST", path=path)
        r = await self._http.post(f"{self._base}{path}", json=body, headers=headers)
        r.raise_for_status()
        filled, _, _ = await self._settle(r.json().get("id"), r.json())
        return net - filled if net > 0 else net + filled


def _dollars_to_micros_safe(value: Any) -> Micros | None:
    if value is None:
        return None
    try:
        return int(round(float(value) * 1_000_000))
    except (TypeError, ValueError):
        return None
