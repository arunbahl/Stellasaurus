from stellasaurus.common.types import (
    OutcomePolarity,
    PairSource,
    PairStatus,
)
from stellasaurus.hot_path.snapshot import AtomicRef, PairRegistryEntry, RegistrySnapshot


def _entry(pair_id: str, status: PairStatus) -> PairRegistryEntry:
    return PairRegistryEntry(
        pair_id=pair_id,
        canonical_proposition="prop",
        kalshi_ticker="KX",
        poly_market_slug="slug",
        outcome_polarity=OutcomePolarity.DIRECT,
        status=status,
        resolves_at_ms=None,
        acceptance_criteria=None,
        last_verified_at_ms=0,
        terms_fingerprint="fp",
        source=PairSource.MANUAL_SEED,
    )


def test_atomic_ref_publish_get():
    ref: AtomicRef[int] = AtomicRef(1)
    assert ref.get() == 1
    ref.publish(2)
    assert ref.get() == 2


def test_registry_snapshot_filters_verified():
    entries = [
        _entry("a", PairStatus.VERIFIED),
        _entry("b", PairStatus.STALE),
        _entry("c", PairStatus.VERIFIED),
    ]
    snap = RegistrySnapshot.build(7, entries)
    assert snap.version == 7
    assert set(snap.verified) == {"a", "c"}
    assert set(snap.by_id) == {"a", "b", "c"}


def test_empty_snapshot():
    snap = RegistrySnapshot.empty()
    assert snap.verified == () and snap.by_id == {}
