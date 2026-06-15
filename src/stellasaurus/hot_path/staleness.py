"""Freshness / staleness reporting (pure, hot-path).

Phase 1 uses this only to *display* per-pair evaluability on the dashboard. In
Phase 4 the same signal feeds the kill switch's auto-trigger: loss/staleness of
either venue's feed makes a pair non-evaluable and halts new entries (DESIGN §6.5
/ §10). Centralized here so both consumers share one definition of "fresh".

GO-REWRITABLE BOUNDARY: stdlib + ``common`` + sibling hot_path modules only.
"""

from __future__ import annotations

from dataclasses import dataclass

from stellasaurus.common.types import Venue
from stellasaurus.hot_path.state import HotStateStore


@dataclass(frozen=True, slots=True)
class PairFreshness:
    pair_id: str
    evaluable: bool
    kalshi_age_ms: int | None  # None == no book yet
    poly_age_ms: int | None


def pair_freshness(store: HotStateStore, pair_id: str) -> PairFreshness:
    return PairFreshness(
        pair_id=pair_id,
        evaluable=store.is_fresh(pair_id),
        kalshi_age_ms=store.book_age_ms(pair_id, Venue.KALSHI),
        poly_age_ms=store.book_age_ms(pair_id, Venue.POLYMARKET),
    )


def registry_freshness(store: HotStateStore) -> tuple[PairFreshness, ...]:
    """Freshness for every VERIFIED pair in the current registry snapshot."""
    snapshot = store.registry()
    return tuple(pair_freshness(store, pid) for pid in snapshot.verified)
