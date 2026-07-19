"""Catalog sync: enumerate both venues' markets and detect terms changes.

Per cycle:
  1. List markets from each venue client.
  2. Compute a ``terms_fingerprint`` over the acceptance-criteria-relevant fields
     and upsert into ``markets``.
  3. If a market's fingerprint changed and a registry pair references it, flip
     that pair to STALE and re-queue it (DESIGN §6.1 / §10). In Phase 1 the
     "re-queue" is just the STALE mark; Phase 2's equivalence engine consumes it.
  4. Refresh the registry snapshot (so awaiting-catalog pairs become VERIFIED and
     resolution times fill in).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable

from stellasaurus.common.logging import audit, get_logger
from stellasaurus.common.types import PairStatus, Venue
from stellasaurus.storage.audit_repo import AuditRepo
from stellasaurus.storage.markets_repo import MarketRow, MarketsRepo
from stellasaurus.storage.registry_repo import RegistryRepo
from stellasaurus.venues.base import RawMarket, VenueClient, market_fingerprint

_log = get_logger("background.catalog_sync")

# Markets that resolved more than this ago are dropped at sync + pruned.
_RESOLVED_GRACE_MS = 2 * 86_400_000


class CatalogSync:
    def __init__(
        self,
        *,
        clients: dict[Venue, VenueClient],
        markets_repo: MarketsRepo,
        registry_repo: RegistryRepo,
        audit_repo: AuditRepo,
        on_catalog_updated: Callable[[], object],
    ) -> None:
        self._clients = clients
        self._markets = markets_repo
        self._registry = registry_repo
        self._audit = audit_repo
        self._on_updated = on_catalog_updated
        self.last_sync_ms: int | None = None
        self.last_counts: dict[str, int] = {}

    async def sync_priority(self, *, now_ms: int, window_ms: int) -> int:
        """Fast re-sync of everything resolving inside the window: the Kalshi
        SERIES containing near-resolution markets (targeted pulls; picks up
        just-listed siblings like game-day markets) plus a fresh Polymarket
        catalog. Returns the number of Kalshi series refreshed."""
        near = self._markets.near_resolution_native_ids(
            Venue.KALSHI, now_ms=now_ms, window_ms=window_ms
        )
        series = sorted({t.split("-", 1)[0] for t in near})
        kalshi = self._clients[Venue.KALSHI]
        batch: list[RawMarket] = []
        if series and hasattr(kalshi, "list_markets_for_series"):
            try:
                batch += await kalshi.list_markets_for_series(series)
            except Exception as exc:  # noqa: BLE001
                _log.warning("priority_kalshi_failed", error=str(exc))
        try:
            batch += await self._clients[Venue.POLYMARKET].list_markets()
        except Exception as exc:  # noqa: BLE001
            _log.warning("priority_poly_failed", error=str(exc))
        await self._store_batch(batch)
        self._on_updated()
        _log.info("priority_synced", kalshi_series=len(series))
        return len(series)

    async def sync_once(self, venues: set[Venue] | None = None) -> None:
        """Sync the given venues (all when None). The bootstrap sweep passes
        {KALSHI} so looping rotation chunks doesn't re-pull the full Polymarket
        catalog every iteration."""
        for venue, client in self._clients.items():
            if venues is not None and venue not in venues:
                continue
            try:
                markets = await client.list_markets()
            except Exception as exc:  # noqa: BLE001 - one venue failing must not abort
                _log.warning("catalog_list_failed", venue=venue.value, error=str(exc))
                continue
            await self._store_batch(markets)
        self.last_counts = self._markets.count_by_venue()
        from stellasaurus.common.clock import wall_ms

        self.last_sync_ms = wall_ms()
        self._on_updated()
        _log.info("catalog_synced", counts=self.last_counts)

    async def _store_batch(self, markets: list[RawMarket]) -> None:
        """Fingerprint + batch-upsert in a worker thread so the event loop keeps
        serving the WS feeds (per-row commits at catalog scale starved them,
        rotting books while freshness looked fine — found via Stage-2 misses)."""
        # Drop already-resolved markets: Kalshi's list endpoint returns the full
        # HISTORY (350k+ rows, 68% dead), and keeping them bloats every pairing
        # cycle's load+match until it blocks the loop. A market resolving inside
        # the grace window is kept so late settlement/pairing still works.
        from stellasaurus.common.clock import wall_ms as _now
        cutoff = _now() - _RESOLVED_GRACE_MS
        markets = [
            m for m in markets
            if m.resolves_at_ms is None or m.resolves_at_ms >= cutoff
        ]
        if not markets:
            return
        # Build rows (fingerprint + json.dumps of raw) AND upsert entirely in the
        # worker thread: at catalog scale (~40k markets) doing the json.dumps on
        # the event loop blocked it for seconds and starved the WS keepalive ping
        # -> reconnect storm -> unresponsive app.
        def _build_and_upsert() -> None:
            rows = [
                MarketRow(
                    venue=m.venue, native_id=m.native_id, title=m.title,
                    rules_text=m.rules_text, settlement_source=m.settlement_source,
                    resolves_at_ms=m.resolves_at_ms, status=m.status,
                    terms_fingerprint=market_fingerprint(m),
                    # Persist raw fields so matchers can read structured data
                    # (Polymarket `outcomes`, Kalshi `yes_sub_title`) for versus.
                    raw_json=json.dumps(m.raw, default=str),
                )
                for m in markets
            ]
            changed = self._markets.upsert_many(rows)
            # STALE-flagging stays in the worker too: interleaving these writes
            # from the LOOP with the worker's transactions made the loop block
            # on the SQLite write lock (the residual hang after the first fix).
            for venue_str, native_id in changed:
                kt = native_id if venue_str == Venue.KALSHI.value else None
                ps = native_id if venue_str == Venue.POLYMARKET.value else None
                for pair_id in self._registry.pairs_referencing(
                    kalshi_ticker=kt, poly_slug=ps
                ):
                    self._registry.set_status(pair_id, PairStatus.STALE)
                    audit(self._audit, actor="catalog_sync", event_type="TERMS_CHANGED",
                          pair_id=pair_id, venue=venue_str, native_id=native_id)

        await asyncio.to_thread(_build_and_upsert)

    async def prune_once(self) -> int:
        """Periodically delete markets resolved beyond the grace window so the
        catalog stays bounded (Kalshi accumulates otherwise). Registry-referenced
        legs are never pruned. Runs entirely in a worker thread."""
        from stellasaurus.common.clock import wall_ms as _now
        cutoff = _now() - _RESOLVED_GRACE_MS

        def _prune() -> int:
            keep = frozenset(
                nid
                for e in self._registry.all_entries()
                for nid in (e.kalshi_ticker, e.poly_market_slug)
            )
            return self._markets.prune_resolved(cutoff_ms=cutoff, keep_native_ids=keep)

        deleted = await asyncio.to_thread(_prune)
        if deleted:
            _log.info("catalog_pruned", deleted=deleted)
        return deleted

    def _upsert(self, m: RawMarket) -> None:
        fp = market_fingerprint(m)
        prior_fp = self._markets.upsert(
            MarketRow(
                venue=m.venue,
                native_id=m.native_id,
                title=m.title,
                rules_text=m.rules_text,
                settlement_source=m.settlement_source,
                resolves_at_ms=m.resolves_at_ms,
                status=m.status,
                terms_fingerprint=fp,
            )
        )
        if prior_fp is None:
            return  # new market or unchanged terms
        # Terms changed: flag any referencing registry pairs STALE.
        kt = m.native_id if m.venue is Venue.KALSHI else None
        ps = m.native_id if m.venue is Venue.POLYMARKET else None
        for pair_id in self._registry.pairs_referencing(kalshi_ticker=kt, poly_slug=ps):
            self._registry.set_status(pair_id, PairStatus.STALE)
            audit(
                self._audit,
                actor="catalog_sync",
                event_type="TERMS_CHANGED",
                pair_id=pair_id,
                venue=m.venue.value,
                native_id=m.native_id,
                old_fingerprint=prior_fp,
                new_fingerprint=fp,
            )
