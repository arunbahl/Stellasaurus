"""Dashboard read model — builds plain dicts from hot state + repos.

Reads only lock-free ``HotState`` accessors (each an ``AtomicRef.get()`` returning
an immutable object), so it can never stall the ingestion path. Money is rendered
to dollar strings here, at the display boundary.
"""

from __future__ import annotations

from typing import Any

from stellasaurus.common.money import PAYOUT_MICROS, micros_to_str
from stellasaurus.common.types import Venue
from stellasaurus.hot_path.book import NormalizedBook
from stellasaurus.hot_path.staleness import registry_freshness
from stellasaurus.hot_path.state import HotStateStore


class ReadModel:
    def __init__(self, store: HotStateStore) -> None:
        self._store = store
        # set by the composition root after construction
        self.feed_stats_provider: Any = lambda: []
        self.catalog_stats_provider: Any = lambda: {}

    # --- health ---
    def health(self) -> dict[str, Any]:
        feeds = [
            {
                "venue": s.venue.value,
                "transport": s.transport,
                "connected": s.connected,
                "frames": s.frames,
                "reconnects": s.reconnects,
                "last_frame_ms": s.last_frame_ms,
            }
            for s in self.feed_stats_provider()
        ]
        freshness = [
            {
                "pair_id": f.pair_id,
                "evaluable": f.evaluable,
                "kalshi_age_ms": f.kalshi_age_ms,
                "poly_age_ms": f.poly_age_ms,
            }
            for f in registry_freshness(self._store)
        ]
        return {"feeds": feeds, "freshness": freshness}

    # --- registry ---
    def pairs(self) -> list[dict[str, Any]]:
        snapshot = self._store.registry()
        return [
            {
                "pair_id": e.pair_id,
                "canonical_proposition": e.canonical_proposition,
                "kalshi_ticker": e.kalshi_ticker,
                "poly_market_slug": e.poly_market_slug,
                "outcome_polarity": e.outcome_polarity.value,
                "status": e.status.value,
                "resolves_at_ms": e.resolves_at_ms,
                "source": e.source.value,
                "terms_fingerprint": e.terms_fingerprint[:12],
            }
            for e in snapshot.by_id.values()
        ]

    # --- catalog ---
    def catalog_stats(self) -> dict[str, Any]:
        return dict(self.catalog_stats_provider())

    # --- positions + risk (Phase 4, paper) ---
    def positions(self) -> dict[str, Any]:
        store = getattr(self, "positions_store", None)
        risk = getattr(self, "risk_manager", None)
        if store is None:
            return {"totals": {}, "open": [], "decisions": []}
        t = store.totals()
        pnl_provider = getattr(self, "pnl_totals_provider", None)
        pnl = pnl_provider() if pnl_provider else {}
        return {
            "totals": {
                "open_pairs": t.open_pairs,
                "committed": micros_to_str(t.committed_micros),
                "unwind_count": t.unwind_count,
                "unwind_loss": micros_to_str(t.unwind_loss_micros),
                "halted": self._store.limits().halted,
                "settled": pnl.get("settled", 0),
                "realized_pnl": micros_to_str(pnl.get("realized_micros", 0)),
            },
            "open": [
                {
                    "position_id": p.position_id,
                    "pair_id": p.pair_id,
                    "orientation": p.orientation,
                    "qty": p.qty,
                    "yes_price": micros_to_str(p.yes_price_micros),
                    "no_price": (
                        micros_to_str(p.no_price_micros)
                        if p.no_price_micros is not None else None
                    ),
                    "fees": micros_to_str(p.fees_micros),
                    "committed": micros_to_str(p.committed_micros),
                    "status": p.hedge_status.value,
                    "unwind_loss": (
                        micros_to_str(p.unwind_loss_micros)
                        if p.unwind_loss_micros is not None else None
                    ),
                    "opened_ms": p.opened_wall_ms,
                }
                for p in store.open_positions()
            ],
            "decisions": [
                {
                    "pair_id": d.pair_id, "orientation": d.orientation,
                    "qty": d.qty, "approved": d.approved,
                    "rejected_by": d.rejected_by, "ts_ms": d.ts_wall_ms,
                }
                for d in (risk.decisions() if risk else ())
            ][-25:],
        }

    # --- paper opportunities (Phase 3) ---
    def opportunities(self) -> dict[str, Any]:
        sink = getattr(self, "opportunity_sink", None)
        if sink is None:
            return {"latest": [], "fired": []}
        latest = sorted(
            sink.latest(),
            key=lambda o: (not o.would_fire, -(o.net_edge_micros or -10**12)),
        )
        return {
            "latest": [_opp_view(o) for o in latest],
            "fired": [_opp_view(o) for o in reversed(sink.fired())][:50],
        }

    # --- books ---
    def book_view(self, pair_id: str) -> dict[str, Any]:
        kalshi = self._store.book(pair_id, Venue.KALSHI)
        poly = self._store.book(pair_id, Venue.POLYMARKET)
        return {
            "pair_id": pair_id,
            "evaluable": self._store.is_fresh(pair_id),
            "kalshi": _book_side(kalshi),
            "polymarket": _book_side(poly),
            "orientations": _orientations(kalshi, poly),
        }

    def all_book_views(self) -> list[dict[str, Any]]:
        return [self.book_view(pid) for pid in self._store.registry().verified]


def _opp_view(o: Any) -> dict[str, Any]:
    return {
        "pair_id": o.pair_id,
        "orientation": o.orientation,
        "yes_venue": o.yes_venue.value,
        "no_venue": o.no_venue.value,
        "would_fire": o.would_fire,
        "gate_failed": o.gate_failed,
        "qty": o.qty,
        "vwap_yes": micros_to_str(o.vwap_yes_micros) if o.vwap_yes_micros is not None else None,
        "vwap_no": micros_to_str(o.vwap_no_micros) if o.vwap_no_micros is not None else None,
        "fees_per_pair": (
            micros_to_str(o.fees_per_pair_micros) if o.fees_per_pair_micros is not None else None
        ),
        "net_edge": micros_to_str(o.net_edge_micros) if o.net_edge_micros is not None else None,
        "net_edge_micros": o.net_edge_micros,
        "t_days": round(o.t_days, 2) if o.t_days is not None else None,
        "annualized_return": (
            round(o.annualized_return, 3) if o.annualized_return is not None else None
        ),
        "ts_ms": o.created_wall_ms,
    }


def _book_side(book: NormalizedBook | None) -> dict[str, Any]:
    if book is None:
        return {"present": False}
    yb, ya = book.best_yes_bid, book.best_yes_ask
    nb, na = book.best_no_bid, book.best_no_ask
    return {
        "present": True,
        "yes_bid": micros_to_str(yb.price) if yb else None,
        "yes_ask": micros_to_str(ya.price) if ya else None,
        "no_bid": micros_to_str(nb.price) if nb else None,
        "no_ask": micros_to_str(na.price) if na else None,
        "yes_side_source": book.yes_side_source.value,
        "no_side_source": book.no_side_source.value,
        "age_ms": book.recv_wall_ms,
    }


def _gross_edge(yes_ask_micros: int | None, no_ask_micros: int | None) -> dict[str, Any] | None:
    """DISPLAY ONLY — not an evaluator firing. gross_edge = $1 - (yes_ask + no_ask).
    Fees, depth, and gates are NOT applied here (those arrive in Phase 3)."""
    if yes_ask_micros is None or no_ask_micros is None:
        return None
    cost = yes_ask_micros + no_ask_micros
    edge = PAYOUT_MICROS - cost
    return {
        "cost": micros_to_str(cost),
        "gross_edge": micros_to_str(edge),
        "gross_edge_micros": edge,
    }


def _orientations(
    kalshi: NormalizedBook | None, poly: NormalizedBook | None
) -> dict[str, Any]:
    k_yes_ask = kalshi.best_yes_ask.price if kalshi and kalshi.best_yes_ask else None
    k_no_ask = kalshi.best_no_ask.price if kalshi and kalshi.best_no_ask else None
    p_yes_ask = poly.best_yes_ask.price if poly and poly.best_yes_ask else None
    p_no_ask = poly.best_no_ask.price if poly and poly.best_no_ask else None
    return {
        # A: buy canonical YES on Kalshi, buy canonical NO on Polymarket
        "A_kalshiYES_polyNO": _gross_edge(k_yes_ask, p_no_ask),
        # B: buy canonical YES on Polymarket, buy canonical NO on Kalshi
        "B_polyYES_kalshiNO": _gross_edge(p_yes_ask, k_no_ask),
    }
