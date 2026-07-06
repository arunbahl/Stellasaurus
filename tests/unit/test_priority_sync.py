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
