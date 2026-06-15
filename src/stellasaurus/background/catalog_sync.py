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

from collections.abc import Callable

from stellasaurus.common.logging import audit, get_logger
from stellasaurus.common.types import PairStatus, Venue
from stellasaurus.storage.audit_repo import AuditRepo
from stellasaurus.storage.markets_repo import MarketRow, MarketsRepo
from stellasaurus.storage.registry_repo import RegistryRepo
from stellasaurus.venues.base import RawMarket, VenueClient, market_fingerprint

_log = get_logger("background.catalog_sync")


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

    async def sync_once(self) -> None:
        for venue, client in self._clients.items():
            try:
                markets = await client.list_markets()
            except Exception as exc:  # noqa: BLE001 - one venue failing must not abort
                _log.warning("catalog_list_failed", venue=venue.value, error=str(exc))
                continue
            for m in markets:
                self._upsert(m)
        self.last_counts = self._markets.count_by_venue()
        from stellasaurus.common.clock import wall_ms

        self.last_sync_ms = wall_ms()
        self._on_updated()
        _log.info("catalog_synced", counts=self.last_counts)

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
