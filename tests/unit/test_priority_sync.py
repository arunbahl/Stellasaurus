"""Near-resolution priority sweep: series selection + targeted refresh."""

from stellasaurus.background.catalog_sync import CatalogSync
from stellasaurus.common.types import Venue
from stellasaurus.storage.audit_repo import AuditRepo
from stellasaurus.storage.db import Database
from stellasaurus.storage.markets_repo import MarketRow, MarketsRepo
from stellasaurus.storage.registry_repo import RegistryRepo
from stellasaurus.venues.base import RawMarket

NOW = 1_999_000_000_000
HOUR = 3_600_000


def _row(venue, nid, resolves):
    return MarketRow(venue, nid, "t", "r", None, resolves, "open", "fp")


class PriorityFakeKalshi:
    venue = Venue.KALSHI

    def __init__(self):
        self.requested_series: list[str] = []

    async def list_markets_for_series(self, series):
        self.requested_series = list(series)
        # a just-listed sibling market appears in the refreshed series
        return [RawMarket(Venue.KALSHI, "KXMLBGAME-NEW", "New game", "r",
                          None, NOW + 2 * HOUR, "open", {})]

    async def list_markets(self):
        return []

    async def get_market(self, nid):
        return None

    async def get_book(self, nid):
        return None


class PriorityFakePoly:
    venue = Venue.POLYMARKET

    async def list_markets(self):
        return []

    async def get_market(self, nid):
        return None

    async def get_book(self, nid):
        return None


async def test_priority_sync_targets_near_resolution_series(tmp_path):
    db = Database(tmp_path / "t.db")
    db.migrate()
    markets = MarketsRepo(db)
    # imminent market (2h), far market (10d), other-venue market (2h)
    markets.upsert(_row(Venue.KALSHI, "KXMLBGAME-OLD", NOW + 2 * HOUR))
    markets.upsert(_row(Venue.KALSHI, "KXFAR-X", NOW + 240 * HOUR))
    markets.upsert(_row(Venue.POLYMARKET, "slug-soon", NOW + 2 * HOUR))
    kalshi = PriorityFakeKalshi()
    sync = CatalogSync(
        clients={Venue.KALSHI: kalshi, Venue.POLYMARKET: PriorityFakePoly()},
        markets_repo=markets, registry_repo=RegistryRepo(db),
        audit_repo=AuditRepo(db), on_catalog_updated=lambda: None,
    )
    n = await sync.sync_priority(now_ms=NOW, window_ms=24 * HOUR)
    assert n == 1
    assert kalshi.requested_series == ["KXMLBGAME"]  # far series excluded
    # the just-listed sibling landed in the catalog
    assert markets.get(Venue.KALSHI, "KXMLBGAME-NEW") is not None


def test_prune_resolved_keeps_registry_legs_and_window(tmp_path):
    db = Database(tmp_path / "t.db")
    db.migrate()
    markets = MarketsRepo(db)
    markets.upsert(_row(Venue.KALSHI, "DEAD-1", NOW - 10 * 24 * HOUR))   # prunable
    markets.upsert(_row(Venue.KALSHI, "DEAD-KEPT", NOW - 10 * 24 * HOUR))  # registry leg
    markets.upsert(_row(Venue.KALSHI, "RECENT", NOW - HOUR))             # inside grace
    markets.upsert(_row(Venue.KALSHI, "LIVE", NOW + HOUR))               # future
    deleted = markets.prune_resolved(
        cutoff_ms=NOW - 2 * 24 * HOUR, keep_native_ids=frozenset({"DEAD-KEPT"})
    )
    assert deleted == 1
    assert markets.get(Venue.KALSHI, "DEAD-1") is None
    assert markets.get(Venue.KALSHI, "DEAD-KEPT") is not None
    assert markets.get(Venue.KALSHI, "RECENT") is not None
    assert markets.get(Venue.KALSHI, "LIVE") is not None


def test_unresolved_markets_bounded_by_horizon(tmp_path):
    db = Database(tmp_path / "t.db")
    db.migrate()
    markets = MarketsRepo(db)
    markets.upsert(_row(Venue.KALSHI, "SOON", NOW + 24 * HOUR))
    markets.upsert(_row(Venue.KALSHI, "FAR", NOW + 400 * 24 * HOUR))  # beyond horizon
    rows = markets.unresolved_markets(
        Venue.KALSHI, now_ms=NOW, horizon_ms=180 * 24 * HOUR
    )
    assert [r.native_id for r in rows] == ["SOON"]
    # no horizon -> both
    assert len(markets.unresolved_markets(Venue.KALSHI, now_ms=NOW)) == 2
