"""Fee Param Sync + reconciliation (DESIGN §6.4 / §6.10).

Periodically refreshes the fee parameters the hot path prices with:

  * Kalshi: ``GET /series/fee_changes`` -> per-series multiplier overrides on
    top of the 0.07 baseline.
  * Polymarket: per-market ``feeCoefficient`` for every market referenced by a
    VERIFIED pair (the venue changed its whole schedule in March 2026 — fees
    are a moving target, which is why this loop exists).

Changes are published atomically to the evaluator and executor, and audited
with old/new values. A change whose worst-case per-order impact (measured on a
reference 100 @ $0.50 trade) exceeds ``fee_divergence_tolerance`` trips the
kill switch: the fee model just moved materially, so nothing should fire until
a human confirms the new schedule.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

import httpx

from stellasaurus.background.halt import HaltController
from stellasaurus.common.config import Settings
from stellasaurus.common.logging import audit, get_logger
from stellasaurus.common.money import micros_to_str
from stellasaurus.common.types import Venue
from stellasaurus.hot_path.fees import FeeParams, reference_fee_delta_micros
from stellasaurus.hot_path.state import HotStateStore
from stellasaurus.storage.audit_repo import AuditRepo
from stellasaurus.venues.base import VenueClient

_log = get_logger("background.fee_sync")


class FeeParamSync:
    def __init__(
        self,
        *,
        settings: Settings,
        http: httpx.AsyncClient,
        clients: dict[Venue, VenueClient],
        store: HotStateStore,
        initial: FeeParams,
        publish_to: list[object],  # objects exposing publish_fee_params(FeeParams)
        halt: HaltController,
        audit_repo: AuditRepo,
    ) -> None:
        self._settings = settings
        self._http = http
        self._clients = clients
        self._store = store
        self._current = initial
        self._publish_to = publish_to
        self._halt = halt
        self._audit = audit_repo
        self._tolerance = settings.fee_divergence_tolerance_micros

    @property
    def current(self) -> FeeParams:
        return self._current

    async def _kalshi_series_overrides(self) -> dict[str, Decimal]:
        r = await self._http.get(
            f"{self._settings.kalshi_rest_base}/series/fee_changes",
            params={"show_historical": "false"},
        )
        if r.status_code != 200:
            _log.warning("kalshi_fee_changes_failed", status=r.status_code)
            return dict(self._current.kalshi_series_multipliers)
        rows = r.json().get("series_fee_change_arr") or r.json().get("fee_changes") or []
        out: dict[str, Decimal] = {}
        for x in rows:
            ticker, mult = x.get("series_ticker"), x.get("fee_multiplier")
            if ticker and mult is not None:
                try:
                    out[str(ticker)] = Decimal(str(mult))
                except InvalidOperation:
                    continue
        return out

    async def _poly_market_coefficients(self) -> dict[str, Decimal]:
        """feeCoefficient for every market referenced by a VERIFIED pair
        (fetched via the signed venue client; the raw catalog payload carries it)."""
        registry = self._store.registry()
        slugs = {registry.by_id[pid].poly_market_slug for pid in registry.verified}
        client = self._clients[Venue.POLYMARKET]
        out: dict[str, Decimal] = {}
        for slug in slugs:
            try:
                m = await client.get_market(slug)
                coeff = (m.raw or {}).get("feeCoefficient") if m else None
                if coeff is not None:
                    out[slug] = Decimal(str(coeff))
            except (httpx.HTTPError, InvalidOperation, ValueError):
                continue
        return out

    async def sync_once(self) -> None:
        old = self._current
        new = FeeParams(
            kalshi_taker_multiplier=old.kalshi_taker_multiplier,
            kalshi_maker_multiplier=old.kalshi_maker_multiplier,
            kalshi_precision_micros=old.kalshi_precision_micros,
            poly_taker_coefficient=old.poly_taker_coefficient,
            poly_maker_coefficient=old.poly_maker_coefficient,
            kalshi_series_multipliers=await self._kalshi_series_overrides(),
            poly_market_coefficients=await self._poly_market_coefficients(),
        )
        delta = reference_fee_delta_micros(old, new)
        if delta == 0:
            return
        audit(
            self._audit,
            actor="fee_sync",
            event_type="FEE_PARAMS_CHANGED",
            reference_delta=micros_to_str(delta),
            kalshi_overrides=len(new.kalshi_series_multipliers),
            poly_overrides=len(new.poly_market_coefficients),
        )
        self._current = new
        for target in self._publish_to:
            target.publish_fee_params(new)  # type: ignore[attr-defined]
        if delta > self._tolerance:
            self._halt.set_halted(
                True, actor="auto",
                reason=f"fee divergence {micros_to_str(delta)} exceeds tolerance "
                       f"{micros_to_str(self._tolerance)} on reference trade",
            )
