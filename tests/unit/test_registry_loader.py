"""RegistryLoader.resolve_seed_markets: direct per-market lookup + verification."""

import textwrap

from stellasaurus.background.registry_loader import RegistryLoader
from stellasaurus.common.types import PairStatus, Venue
from stellasaurus.hot_path.snapshot import LimitsSnapshot, RegistrySnapshot
from stellasaurus.hot_path.state import HotStateStore
from stellasaurus.storage.audit_repo import AuditRepo
from stellasaurus.storage.db import Database
from stellasaurus.storage.markets_repo import MarketsRepo
from stellasaurus.storage.registry_repo import RegistryRepo
from stellasaurus.venues.base import RawMarket


class FakeClient:
    def __init__(self, venue: Venue, known: dict[str, RawMarket]) -> None:
        self.venue = venue
        self._known = known

    async def list_markets(self):
        return list(self._known.values())

    async def get_market(self, native_id: str) -> RawMarket | None:
        return self._known.get(native_id)

    async def get_book(self, native_id: str):
        return None


def _market(venue: Venue, nid: str) -> RawMarket:
    return RawMarket(venue, nid, "Title", "rules", "src", 1_900_000_000_000, "open", {})


def _loader(tmp_path, seed_text: str):
    seed = tmp_path / "seed.yaml"
    seed.write_text(textwrap.dedent(seed_text))
    db = Database(tmp_path / "t.db")
    db.migrate()
    store = HotStateStore(
        registry=RegistrySnapshot.empty(),
        limits=LimitsSnapshot(1, True, 0, 0.0, 1, 1, 1, 1, 1, 1, 0.5),
        book_staleness_ms=2000,
    )
    loader = RegistryLoader(
        seed_path=seed,
        registry_repo=RegistryRepo(db),
        markets_repo=MarketsRepo(db),
        audit_repo=AuditRepo(db),
        store=store,
    )
    return loader, store


SEED = """
pairs:
  - pair_id: p1
    canonical_proposition: "X happens"
    kalshi_ticker: KX1
    poly_market_slug: slug1
    outcome_polarity: DIRECT
    status: VERIFIED
"""


async def test_both_legs_resolve_marks_verified(tmp_path):
    loader, store = _loader(tmp_path, SEED)
    clients = {
        Venue.KALSHI: FakeClient(Venue.KALSHI, {"KX1": _market(Venue.KALSHI, "KX1")}),
        Venue.POLYMARKET: FakeClient(
            Venue.POLYMARKET, {"slug1": _market(Venue.POLYMARKET, "slug1")}
        ),
    }
    resolved = await loader.resolve_seed_markets(clients)
    assert resolved == 2
    snap = store.registry()
    assert snap.by_id["p1"].status is PairStatus.VERIFIED
    assert snap.by_id["p1"].resolves_at_ms == 1_900_000_000_000
    assert "p1" in snap.verified


async def test_missing_poly_leg_stays_stale(tmp_path):
    loader, store = _loader(tmp_path, SEED)
    clients = {
        Venue.KALSHI: FakeClient(Venue.KALSHI, {"KX1": _market(Venue.KALSHI, "KX1")}),
        Venue.POLYMARKET: FakeClient(Venue.POLYMARKET, {}),  # poly unreachable / not found
    }
    resolved = await loader.resolve_seed_markets(clients)
    assert resolved == 1  # only the Kalshi leg
    snap = store.registry()
    assert snap.by_id["p1"].status is PairStatus.STALE
    assert snap.verified == ()
