"""Requote-survival probe — the mirage filter.

A would-fire edge on the STREAMED books can be a feed-lag mirage: during a fast
move one venue's book lags the other within the freshness window, so an apparent
gap evaporates the instant the lagging feed catches up. The live engine already
guards against this by RE-QUOTING (a fresh REST fetch) before firing; this probe
applies the same test out-of-band and logs whether each would-fire edge SURVIVES
a requote — separating real, tradeable dislocations from lag artifacts.

For every pair currently flagged would_fire (rate-limited per pair), it fetches
both venues' books fresh, recomputes the best-orientation net edge exactly as the
evaluator does, and appends {streamed_net, requoted_net, survived} to a JSONL.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from stellasaurus.common.clock import wall_ms
from stellasaurus.common.logging import get_logger
from stellasaurus.common.money import PAYOUT_MICROS
from stellasaurus.common.types import Micros, OutcomePolarity, Venue
from stellasaurus.hot_path.book import walk_book_for_size
from stellasaurus.hot_path.fees import FeeParams, kalshi_fee_micros, poly_fee_micros
from stellasaurus.hot_path.normalize import normalize
from stellasaurus.hot_path.opportunities import OpportunitySink
from stellasaurus.hot_path.state import HotStateStore
from stellasaurus.venues.base import VenueClient

_log = get_logger("background.requote_probe")


class RequoteProbe:
    def __init__(
        self,
        *,
        clients: dict[Venue, VenueClient],
        store: HotStateStore,
        fee_params: FeeParams,
        opp_sink: OpportunitySink,
        log_path: Path,
        theta_micros: Micros,
        min_interval_s: float = 5.0,
        poll_interval_s: float = 2.0,
    ) -> None:
        self._clients = clients
        self._store = store
        self._fees = fee_params
        self._sink = opp_sink
        self._path = log_path
        self._theta = theta_micros
        self._min_interval_ms = int(min_interval_s * 1000)
        self._poll = poll_interval_s
        self._last: dict[str, int] = {}

    def _fee(self, venue: Venue, qty: int, price: Micros, *, series: str, slug: str) -> Micros:
        if venue is Venue.KALSHI:
            return kalshi_fee_micros(qty, price, params=self._fees, series=series)
        return poly_fee_micros(qty, price, params=self._fees, market=slug)

    def _net(
        self, kn: object, pn: object, qty: int, series: str, slug: str
    ) -> tuple[str, Micros] | None:
        """Best-orientation net edge from fresh normalized books (evaluator math)."""
        best: tuple[str, Micros] | None = None
        # A: Kalshi YES + Poly NO ; B: Poly YES + Kalshi NO
        for orient, yv, ya, nv, na in (
            ("A", Venue.KALSHI, kn.yes_asks, Venue.POLYMARKET, pn.no_asks),   # type: ignore[attr-defined]
            ("B", Venue.POLYMARKET, pn.yes_asks, Venue.KALSHI, kn.no_asks),   # type: ignore[attr-defined]
        ):
            vy = walk_book_for_size(ya, qty)
            vn = walk_book_for_size(na, qty)
            if vy is None or vn is None:
                continue
            fy = self._fee(yv, qty, vy, series=series, slug=slug)
            fn = self._fee(nv, qty, vn, series=series, slug=slug)
            fees = (fy + fn + qty - 1) // qty
            net = PAYOUT_MICROS - (vy + vn + fees)
            if best is None or net > best[1]:
                best = (orient, net)
        return best

    async def run(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        while True:
            try:
                await self._probe_once()
            except Exception as exc:  # noqa: BLE001 - never kill the probe
                _log.warning("requote_probe_error", error=str(exc))
            await asyncio.sleep(self._poll)

    async def _probe_once(self) -> None:
        now = wall_ms()
        fires = {
            o.pair_id: o for o in self._sink.latest()
            if o.would_fire and now - self._last.get(o.pair_id, 0) >= self._min_interval_ms
        }
        if not fires:
            return
        reg = self._store.registry()
        lines: list[str] = []
        for pair_id, opp in fires.items():
            self._last[pair_id] = now
            entry = reg.by_id.get(pair_id)
            if entry is None:
                continue
            series = entry.kalshi_ticker.split("-", 1)[0]
            try:
                kb = await self._clients[Venue.KALSHI].get_book(entry.kalshi_ticker)
                pb = await self._clients[Venue.POLYMARKET].get_book(entry.poly_market_slug)
            except Exception as exc:  # noqa: BLE001
                _log.warning("requote_fetch_failed", pair_id=pair_id, error=str(exc))
                continue
            if kb is None or pb is None:
                continue
            kn = normalize(kb, polarity=OutcomePolarity.DIRECT, pair_id=pair_id)
            pn = normalize(pb, polarity=entry.outcome_polarity, pair_id=pair_id)
            fresh = self._net(kn, pn, opp.qty, series, entry.poly_market_slug)
            requoted = fresh[1] if fresh else None
            survived = requoted is not None and requoted >= self._theta
            lines.append(json.dumps({
                "ts": now, "pair": pair_id, "qty": opp.qty,
                "streamed_net": opp.net_edge_micros,
                "requoted_net": requoted,
                "survived": survived,
            }))
        if lines:
            text = "\n".join(lines) + "\n"

            def _write() -> None:
                with self._path.open("a") as f:
                    f.write(text)

            await asyncio.to_thread(_write)
