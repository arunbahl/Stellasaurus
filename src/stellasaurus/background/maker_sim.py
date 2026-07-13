"""Maker/rebate strategy measurement (no real orders).

Simulates resting Polymarket maker orders and measures the two numbers that
decide whether path 1 is viable:

  1. FILL-CONDITIONAL HEDGE ECONOMICS (captures adverse selection). A resting
     Poly canonical-YES bid fills when a seller crosses it — i.e. when the best
     bid DOWNTICKS. That is precisely the adverse moment (YES just got less
     likely), and the Kalshi hedge has repriced against you too. On each such
     inferred fill we record: fill price, the Poly maker REBATE earned, the
     Kalshi taker hedge cost right then, and the locked net edge. The
     DISTRIBUTION of that net answers "is making+hedging profitable after
     adverse selection?" — sampling uniformly would overstate it.

  2. HEDGE-RACE DRIFT. Polymarket's fill confirmation lags ~1s, so we can't hedge
     until then. Each fill event is re-checked after ``lag_s`` against the fresh
     Kalshi hedge cost; net_lag vs net_now shows how much the edge decays while
     we wait — whether the cross-venue hedge race is winnable.

Fill inference is from L1 book moves (a bid downtick ≈ a maker at that level was
hit), an approximation — not true fills — but it is adverse-selection-aware,
which is the property that matters.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from stellasaurus.common.clock import wall_ms
from stellasaurus.common.logging import get_logger
from stellasaurus.common.money import PAYOUT_MICROS
from stellasaurus.common.types import Micros, Venue
from stellasaurus.hot_path.book import walk_book_for_size
from stellasaurus.hot_path.fees import FeeParams, kalshi_fee_micros, poly_fee_micros
from stellasaurus.hot_path.state import HotStateStore

_log = get_logger("background.maker_sim")


class MakerSim:
    def __init__(
        self,
        *,
        store: HotStateStore,
        fee_params: FeeParams,
        log_path: Path,
        qty: int = 20,  # realistic maker size; at qty=1 the sub-cent rebate
                        # banker's-rounds to zero and vanishes
        poll_s: float = 0.25,
        lag_s: float = 1.0,
    ) -> None:
        self._store = store
        self._fees = fee_params
        self._path = log_path
        self._qty = qty
        self._poll = poll_s
        self._lag_ms = int(lag_s * 1000)
        self._last_bid: dict[tuple[str, str], Micros] = {}
        self._pending: list[dict[str, object]] = []

    def _net(
        self, fill_px: Micros, hedge_px: Micros, series: str, slug: str
    ) -> tuple[Micros, Micros]:
        """Per-contract (net_edge, rebate) for making YES/NO at fill_px +
        taker-hedging at hedge_px. Poly maker fee is negative (a rebate); Kalshi
        hedge is taker. The fee helpers return the TOTAL fee over ``qty``
        contracts, so both are amortised back to per-contract to match the
        per-contract prices (same convention as the requote probe)."""
        poly_maker = poly_fee_micros(self._qty, fill_px, params=self._fees,
                                     market=slug, is_maker=True)  # <= 0 (rebate)
        kalshi_taker = kalshi_fee_micros(self._qty, hedge_px, params=self._fees,
                                         series=series, is_maker=False)
        # ceil-divide the net fee to per-contract (conservative on cost).
        fee_pc = (poly_maker + kalshi_taker + self._qty - 1) // self._qty
        rebate_pc = (-poly_maker) // self._qty
        net = PAYOUT_MICROS - fill_px - hedge_px - fee_pc
        return net, rebate_pc

    async def run(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        while True:
            try:
                self._tick()
            except Exception as exc:  # noqa: BLE001 - never kill the sim
                _log.warning("maker_sim_error", error=str(exc))
            await asyncio.sleep(self._poll)

    def _tick(self) -> None:
        now = wall_ms()
        reg = self._store.registry()
        # 1) detect maker fills (bid downticks) and enqueue for a lag recheck
        for pair_id in reg.verified:
            entry = reg.by_id.get(pair_id)
            if entry is None or not self._store.is_fresh(pair_id):
                continue
            kn = self._store.book(pair_id, Venue.KALSHI)
            pn = self._store.book(pair_id, Venue.POLYMARKET)
            if kn is None or pn is None:
                continue
            series = entry.kalshi_ticker.split("-", 1)[0]
            for side, bid, hedge_asks in (
                ("YES", pn.best_yes_bid, kn.no_asks),
                ("NO", pn.best_no_bid, kn.yes_asks),
            ):
                if bid is None:
                    continue
                key = (pair_id, side)
                prev = self._last_bid.get(key)
                self._last_bid[key] = bid.price
                if prev is None or bid.price >= prev:
                    continue  # not a downtick -> no adverse maker fill
                hedge = walk_book_for_size(hedge_asks, self._qty)
                if hedge is None:
                    continue
                net_now, rebate = self._net(prev, hedge, series, entry.poly_market_slug)
                self._pending.append({
                    "ts": now, "pair": pair_id, "side": side, "fill_px": prev,
                    "hedge_now": hedge, "rebate": rebate, "net_now": net_now,
                    "series": series, "slug": entry.poly_market_slug,
                    "recheck_at": now + self._lag_ms,
                })
        # 2) lag recheck: re-price the hedge ~1s later (the fill-lag race)
        lines: list[str] = []
        still: list[dict[str, object]] = []
        for e in self._pending:
            if now < int(e["recheck_at"]):  # type: ignore[call-overload]
                still.append(e)
                continue
            kn = self._store.book(str(e["pair"]), Venue.KALSHI)
            hedge_lag = None
            if kn is not None:
                hedge_side = kn.no_asks if e["side"] == "YES" else kn.yes_asks
                hedge_lag = walk_book_for_size(hedge_side, self._qty)
            net_lag = None
            if hedge_lag is not None:
                net_lag, _ = self._net(int(e["fill_px"]), hedge_lag,  # type: ignore[call-overload]
                                       str(e["series"]), str(e["slug"]))
            lines.append(json.dumps({
                "ts": e["ts"], "pair": e["pair"], "side": e["side"],
                "fill_px": e["fill_px"], "rebate": e["rebate"],
                "hedge_now": e["hedge_now"], "net_now": e["net_now"],
                "hedge_lag": hedge_lag, "net_lag": net_lag,
            }))
        self._pending = still
        if lines:
            text = "\n".join(lines) + "\n"

            def _write() -> None:
                with self._path.open("a") as f:
                    f.write(text)

            asyncio.create_task(asyncio.to_thread(_write))  # noqa: RUF006
