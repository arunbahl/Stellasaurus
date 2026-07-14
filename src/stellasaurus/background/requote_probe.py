"""Requote-survival probe — the mirage filter (concurrent + persistence).

A would-fire edge on the STREAMED books can be a feed-lag mirage: during a fast
move one venue's book lags the other within the freshness window, so an apparent
gap evaporates the instant the lagging feed catches up. The live engine already
guards against this by RE-QUOTING (a fresh REST fetch) before firing; this probe
applies the same test out-of-band and logs whether each would-fire edge SURVIVES.

Two hardenings over a naive single requote, aimed at the weather-market signal:

  - CONCURRENT fetch. Both venues' books are pulled with a single
    ``asyncio.gather`` rather than ``await k; await p``. A sequential fetch leaves
    a few-hundred-ms skew between the two snapshots, which in an intraday-volatile
    temperature market (the day's high firming up) can manufacture an edge that
    isn't simultaneously executable. Gather removes that skew.

  - PERSISTENCE. A real, tradeable dislocation persists for more than one instant;
    a timing artifact does not. Each would-fire pair is requoted ``persist_probes``
    times spaced ``persist_spacing_s`` apart, and we record the full net series
    plus how many cleared theta. ``survived_all`` (every probe cleared) is the
    strict, trust-worthy signal.

Appends one JSONL record per probed pair: {streamed_net, requote_nets[],
survived_count, survived_all, span_ms}.
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
from stellasaurus.hot_path.snapshot import PairRegistryEntry
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
        persist_probes: int = 5,
        persist_spacing_s: float = 1.0,
        max_per_cycle: int = 6,
    ) -> None:
        self._clients = clients
        self._store = store
        self._fees = fee_params
        self._sink = opp_sink
        self._path = log_path
        self._theta = theta_micros
        self._min_interval_ms = int(min_interval_s * 1000)
        self._poll = poll_interval_s
        self._persist_probes = max(1, persist_probes)
        self._persist_spacing_s = persist_spacing_s
        self._max_per_cycle = max_per_cycle
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

    async def _requote(
        self, entry: PairRegistryEntry, qty: int, pair_id: str
    ) -> Micros | None:
        """One CONCURRENT requote -> best-orientation net (None on any failure)."""
        try:
            kb, pb = await asyncio.gather(
                self._clients[Venue.KALSHI].get_book(entry.kalshi_ticker),
                self._clients[Venue.POLYMARKET].get_book(entry.poly_market_slug),
            )
        except Exception as exc:  # noqa: BLE001
            _log.warning("requote_fetch_failed", pair_id=pair_id, error=str(exc))
            return None
        if kb is None or pb is None:
            return None
        series = entry.kalshi_ticker.split("-", 1)[0]
        kn = normalize(kb, polarity=OutcomePolarity.DIRECT, pair_id=pair_id)
        pn = normalize(pb, polarity=entry.outcome_polarity, pair_id=pair_id)
        best = self._net(kn, pn, qty, series, entry.poly_market_slug)
        return best[1] if best else None

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
        fires = [
            o for o in self._sink.latest()
            if o.would_fire and now - self._last.get(o.pair_id, 0) >= self._min_interval_ms
        ]
        if not fires:
            return
        # Bound work per cycle: persistence is ~persist_probes*spacing s per pair.
        fires = fires[: self._max_per_cycle]
        reg = self._store.registry()
        lines: list[str] = []
        for opp in fires:
            self._last[opp.pair_id] = now
            entry = reg.by_id.get(opp.pair_id)
            if entry is None:
                continue
            nets: list[Micros | None] = []
            for i in range(self._persist_probes):
                if i:
                    await asyncio.sleep(self._persist_spacing_s)
                nets.append(await self._requote(entry, opp.qty, opp.pair_id))
            cleared = sum(1 for n in nets if n is not None and n >= self._theta)
            lines.append(json.dumps({
                "ts": now, "pair": opp.pair_id, "qty": opp.qty,
                "streamed_net": opp.net_edge_micros,
                "requote_nets": nets,
                "n_probes": len(nets),
                "survived_count": cleared,
                "survived_all": cleared == len(nets) and len(nets) > 0,
                "span_ms": wall_ms() - now,
            }))
        if lines:
            text = "\n".join(lines) + "\n"

            def _write() -> None:
                with self._path.open("a") as f:
                    f.write(text)

            await asyncio.to_thread(_write)
