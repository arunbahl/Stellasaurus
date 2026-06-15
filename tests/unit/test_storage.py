from stellasaurus.common.types import OutcomePolarity, PairSource, PairStatus, Venue
from stellasaurus.hot_path.snapshot import PairRegistryEntry, RegistrySnapshot
from stellasaurus.storage.audit_repo import AuditRepo
from stellasaurus.storage.db import Database
from stellasaurus.storage.markets_repo import MarketRow, MarketsRepo
from stellasaurus.storage.registry_repo import RegistryRepo


def _db(tmp_path) -> Database:
    db = Database(tmp_path / "t.db")
    db.migrate()
    return db


def test_markets_upsert_detects_fingerprint_change(tmp_path):
    repo = MarketsRepo(_db(tmp_path))
    row = MarketRow(Venue.KALSHI, "KX", "t", "rules", None, 1, "open", "fp1")
    assert repo.upsert(row) is None  # new
    assert repo.upsert(row) is None  # unchanged
    changed = MarketRow(Venue.KALSHI, "KX", "t", "rules2", None, 1, "open", "fp2")
    assert repo.upsert(changed) == "fp1"  # returns prior fp on change
    assert repo.count_by_venue() == {"KALSHI": 1}


def test_registry_round_trip_and_snapshot(tmp_path):
    repo = RegistryRepo(_db(tmp_path))
    entry = PairRegistryEntry(
        "p1", "prop", "KX", "slug", OutcomePolarity.INVERTED, PairStatus.VERIFIED,
        123, {"note": "x"}, 111, "fp", PairSource.MANUAL_SEED,
    )
    repo.upsert(entry)
    repo.set_status("p1", PairStatus.STALE)
    entries = repo.all_entries()
    assert len(entries) == 1
    got = entries[0]
    assert got.outcome_polarity is OutcomePolarity.INVERTED
    assert got.status is PairStatus.STALE
    assert got.acceptance_criteria == {"note": "x"}
    snap = RegistrySnapshot.build(1, entries)
    assert snap.verified == ()  # now STALE


def test_pairs_referencing(tmp_path):
    repo = RegistryRepo(_db(tmp_path))
    repo.upsert(
        PairRegistryEntry("p1", "x", "KX1", "slug1", OutcomePolarity.DIRECT,
                          PairStatus.VERIFIED, None, None, 0, "fp", PairSource.MANUAL_SEED)
    )
    assert repo.pairs_referencing(kalshi_ticker="KX1", poly_slug=None) == ["p1"]
    assert repo.pairs_referencing(kalshi_ticker=None, poly_slug="slug1") == ["p1"]
    assert repo.pairs_referencing(kalshi_ticker="nope", poly_slug="nope") == []


def test_audit_append_and_recent(tmp_path):
    repo = AuditRepo(_db(tmp_path))
    repo.append(actor="system", event_type="TEST", pair_id="p1", detail={"a": 1})
    recent = repo.recent()
    assert recent[0]["event_type"] == "TEST"
    assert recent[0]["detail"] == {"a": 1}
