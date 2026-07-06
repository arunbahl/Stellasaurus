"""Pairing loop: candidate generation + LLM dispatch + registry writes (no network)."""

from stellasaurus.background.pairing import PairingLoop, generate_candidates
from stellasaurus.background.registry_loader import RegistryLoader
from stellasaurus.baml_client.types import EquivalenceVerdict
from stellasaurus.baml_client.types import OutcomePolarity as BamlPolarity
from stellasaurus.common.types import PairSource, PairStatus, Venue
from stellasaurus.hot_path.snapshot import LimitsSnapshot, RegistrySnapshot
from stellasaurus.hot_path.state import HotStateStore
from stellasaurus.storage.audit_repo import AuditRepo
from stellasaurus.storage.db import Database
from stellasaurus.storage.markets_repo import MarketsRepo
from stellasaurus.storage.registry_repo import RegistryRepo
from stellasaurus.venues.base import RawMarket

DAY = 86_400_000
T0 = 1_783_400_400_000  # some UTC instant


def _m(venue: Venue, nid: str, title: str, resolves: int = T0, rules: str = "") -> RawMarket:
    return RawMarket(venue, nid, title, rules or title, "src", resolves, "open", {})


def test_candidates_match_same_day_similar_titles():
    k = [_m(Venue.KALSHI, "KXHIGHNY-T84", "Highest temperature in NYC today 83 or below")]
    p = [
        _m(
            Venue.POLYMARKET, "tc-temp-nychigh-lt84f",
            "Highest temperature in NYC today 83 or below",
        ),
        _m(Venue.POLYMARKET, "unrelated-btc", "Bitcoin above 100000 by December"),
    ]
    cands = generate_candidates(k, p)
    assert len(cands) == 1
    assert cands[0].poly.native_id == "tc-temp-nychigh-lt84f"
    assert cands[0].score > 0.9


def test_candidates_reject_different_day():
    k = [_m(Venue.KALSHI, "K1", "Highest temperature in NYC 83 or below", resolves=T0)]
    p = [
        _m(Venue.POLYMARKET, "P1", "Highest temperature in NYC 83 or below",
           resolves=T0 + 3 * DAY)
    ]
    assert generate_candidates(k, p) == []


def test_candidates_reject_numeric_mismatch():
    # heavy token overlap but different thresholds -> must not be a candidate
    k = [_m(Venue.KALSHI, "K1", "Highest temperature in NYC 84 or below today")]
    p = [_m(Venue.POLYMARKET, "P1", "Highest temperature in NYC 90 or below today")]
    assert generate_candidates(k, p) == []


def test_candidates_pick_best_scoring_poly_market():
    k = [_m(Venue.KALSHI, "K1", "Will the Lakers beat the Celtics tonight")]
    p = [
        _m(Venue.POLYMARKET, "P-good", "Lakers beat Celtics tonight"),
        _m(Venue.POLYMARKET, "P-weak", "Lakers season win total tonight maybe"),
    ]
    cands = generate_candidates(k, p)
    assert len(cands) == 1 and cands[0].poly.native_id == "P-good"


class FakeEngine:
    """Deterministic stand-in for the LLM engine (PairingLoop uses the real
    ``EquivalenceEngine.disposition`` statically, so only evaluate() is needed)."""

    configured = True

    def __init__(self, equivalent_ids: set[str]) -> None:
        self._eq = equivalent_ids
        self.calls = 0

    async def evaluate(self, a, b):  # noqa: ANN001
        self.calls += 1
        return EquivalenceVerdict(
            equivalent=a.native_id in self._eq,
            outcome_polarity=BamlPolarity.DIRECT,
            reason="fake",
        )


class FakeClient:
    def __init__(self, venue: Venue, markets: list[RawMarket]) -> None:
        self.venue = venue
        self._markets = markets

    async def list_markets(self):
        return self._markets

    async def get_market(self, native_id):  # noqa: ANN001
        return next((m for m in self._markets if m.native_id == native_id), None)

    async def get_book(self, native_id):  # noqa: ANN001
        return None


def _fixture(tmp_path):
    db = Database(tmp_path / "t.db")
    db.migrate()
    registry = RegistryRepo(db)
    store = HotStateStore(
        registry=RegistrySnapshot.empty(),
        limits=LimitsSnapshot(1, True, 0, 0.0, 1, 1, 1, 1, 1, 1, 0.5),
        book_staleness_ms=2000,
    )
    loader = RegistryLoader(
        seed_path=tmp_path / "no-seed.yaml",
        registry_repo=registry,
        markets_repo=MarketsRepo(db),
        audit_repo=AuditRepo(db),
        store=store,
    )
    return registry, AuditRepo(db), loader, store


async def test_pairing_loop_writes_llm_rows_and_publishes(tmp_path):
    registry, audit_repo, loader, store = _fixture(tmp_path)
    k_eq = _m(Venue.KALSHI, "K-EQ", "Highest temperature in NYC 83 or below today")
    k_neq = _m(Venue.KALSHI, "K-NEQ", "Will the Lakers beat the Celtics tonight")
    clients = {
        Venue.KALSHI: FakeClient(Venue.KALSHI, [k_eq, k_neq]),
        Venue.POLYMARKET: FakeClient(Venue.POLYMARKET, [
            _m(Venue.POLYMARKET, "P-EQ", "Highest temperature in NYC 83 or below today"),
            _m(Venue.POLYMARKET, "P-NEQ", "Lakers beat Celtics tonight"),
        ]),
    }
    engine = FakeEngine(equivalent_ids={"K-EQ"})
    loop = PairingLoop(
        clients=clients, engine=engine, registry_repo=registry,
        audit_repo=audit_repo, publish=loader.publish, max_llm_calls=10,
    )
    evaluated = await loop.run_once()
    assert evaluated == 2
    entries = {e.kalshi_ticker: e for e in registry.all_entries()}
    assert entries["K-EQ"].status is PairStatus.VERIFIED
    assert entries["K-EQ"].source is PairSource.LLM
    assert entries["K-NEQ"].status is PairStatus.NOT_EQUIVALENT
    # snapshot published with only the VERIFIED pair streamed
    snap = store.registry()
    assert len(snap.verified) == 1

    # second cycle: both leg-pairs already judged -> zero new LLM calls
    assert await loop.run_once() == 0
    assert engine.calls == 2


async def test_pairing_loop_respects_llm_budget(tmp_path):
    registry, audit_repo, loader, _ = _fixture(tmp_path)
    ks = [_m(Venue.KALSHI, f"K{i}", f"Event number {i} resolves in NYC today") for i in range(5)]
    ps = [
        _m(Venue.POLYMARKET, f"P{i}", f"Event number {i} resolves in NYC today")
        for i in range(5)
    ]
    clients = {
        Venue.KALSHI: FakeClient(Venue.KALSHI, ks),
        Venue.POLYMARKET: FakeClient(Venue.POLYMARKET, ps),
    }
    engine = FakeEngine(equivalent_ids=set())
    loop = PairingLoop(
        clients=clients, engine=engine, registry_repo=registry,
        audit_repo=audit_repo, publish=loader.publish, max_llm_calls=2,
    )
    assert await loop.run_once() == 2
    assert engine.calls == 2
