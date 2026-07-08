"""Pairing loop: candidate generation + LLM dispatch + registry writes (no network)."""

from stellasaurus.background.pairing import PairingLoop, generate_candidates
from stellasaurus.background.registry_loader import RegistryLoader
from stellasaurus.baml_client.types import EquivalenceVerdict
from stellasaurus.baml_client.types import OutcomePolarity as BamlPolarity
from stellasaurus.common.types import OutcomePolarity, PairSource, PairStatus, Venue
from stellasaurus.hot_path.snapshot import LimitsSnapshot, RegistrySnapshot
from stellasaurus.hot_path.state import HotStateStore
from stellasaurus.storage.audit_repo import AuditRepo
from stellasaurus.storage.db import Database
from stellasaurus.storage.markets_repo import MarketsRepo
from stellasaurus.storage.registry_repo import RegistryRepo
from stellasaurus.venues.base import RawMarket

DAY = 86_400_000
T0 = 1_999_000_000_000  # far-future UTC instant (markets must be unresolved)


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
    markets = MarketsRepo(db)
    store = HotStateStore(
        registry=RegistrySnapshot.empty(),
        limits=LimitsSnapshot(1, True, 0, 0.0, 1, 1, 1, 1, 1, 1, 0.5),
        book_staleness_ms=2000,
    )
    loader = RegistryLoader(
        seed_path=tmp_path / "no-seed.yaml",
        registry_repo=registry,
        markets_repo=markets,
        audit_repo=AuditRepo(db),
        store=store,
    )
    return registry, markets, AuditRepo(db), loader, store


def _catalog(markets_repo, ms):
    from stellasaurus.storage.markets_repo import MarketRow
    from stellasaurus.venues.base import market_fingerprint
    for m in ms:
        markets_repo.upsert(MarketRow(
            venue=m.venue, native_id=m.native_id, title=m.title, rules_text=m.rules_text,
            settlement_source=m.settlement_source, resolves_at_ms=m.resolves_at_ms,
            status=m.status, terms_fingerprint=market_fingerprint(m),
        ))


async def test_pairing_loop_writes_llm_rows_and_publishes(tmp_path):
    registry, markets, audit_repo, loader, store = _fixture(tmp_path)
    k_eq = _m(Venue.KALSHI, "K-EQ", "Highest temperature in NYC 83 or below today")
    k_neq = _m(Venue.KALSHI, "K-NEQ", "Will the Lakers beat the Celtics tonight")
    _catalog(markets, [
        k_eq, k_neq,
        _m(Venue.POLYMARKET, "P-EQ", "Highest temperature in NYC 83 or below today"),
        _m(Venue.POLYMARKET, "P-NEQ", "Lakers beat Celtics tonight"),
    ])
    engine = FakeEngine(equivalent_ids={"K-EQ"})
    loop = PairingLoop(
        markets_repo=markets, engine=engine, registry_repo=registry,
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
    registry, markets, audit_repo, loader, _ = _fixture(tmp_path)
    ks = [_m(Venue.KALSHI, f"K{i}", f"Event number {i} resolves in NYC today") for i in range(5)]
    ps = [
        _m(Venue.POLYMARKET, f"P{i}", f"Event number {i} resolves in NYC today")
        for i in range(5)
    ]
    _catalog(markets, ks + ps)
    engine = FakeEngine(equivalent_ids=set())
    loop = PairingLoop(
        markets_repo=markets, engine=engine, registry_repo=registry,
        audit_repo=audit_repo, publish=loader.publish, max_llm_calls=2,
    )
    assert await loop.run_once() == 2
    assert engine.calls == 2


async def test_structured_only_pass_spends_no_llm(tmp_path):
    registry, markets, audit_repo, loader, store = _fixture(tmp_path)
    _catalog(markets, [
        _m(Venue.KALSHI, "K1", "Highest temperature in NYC 83 or below today"),
        _m(Venue.POLYMARKET, "P1", "Highest temperature in NYC 83 or below today"),
    ])
    engine = FakeEngine(equivalent_ids={"K1"})
    loop = PairingLoop(
        markets_repo=markets, engine=engine, registry_repo=registry,
        audit_repo=audit_repo, publish=loader.publish, max_llm_calls=10,
    )
    evaluated = await loop.run_once(llm_budget=0)
    assert evaluated == 0
    assert engine.calls == 0  # nothing spent on the LLM


async def test_versus_polarity_override_and_ambiguous_reject(tmp_path):
    """Item 2b: LLM judges equivalence; the deterministic resolver OVERRIDES its
    polarity for versus markets, and an unresolvable polarity is REJECTED
    without an LLM call."""
    import json as _json

    from stellasaurus.storage.markets_repo import MarketRow
    from stellasaurus.venues.base import market_fingerprint
    registry, markets, audit_repo, loader, store = _fixture(tmp_path)

    def put(venue, nid, title, raw):
        m = RawMarket(venue, nid, title, title, "src", T0, "open", raw)
        markets.upsert(MarketRow(
            venue=venue, native_id=nid, title=title, rules_text=title,
            settlement_source="src", resolves_at_ms=T0, status="open",
            terms_fingerprint=market_fingerprint(m), raw_json=_json.dumps(raw)))

    # (1) versus pair the resolver reads as INVERTED (Kalshi-YES = outcomes[1])
    put(Venue.KALSHI, "K-UFC", "Conor McGregor vs Max Holloway winner",
        {"yes_sub_title": "Conor McGregor", "rules_primary": "If Conor McGregor wins"})
    put(Venue.POLYMARKET, "P-UFC", "Conor McGregor vs Max Holloway winner",
        {"outcomes": _json.dumps(["Max Holloway", "Conor McGregor"])})
    # (2) versus pair whose YES entity matches NEITHER outcome -> ambiguous
    put(Venue.KALSHI, "K-WNBA", "WNBA Golden State vs Toronto winner today",
        {"yes_sub_title": "Golden State", "rules_primary": "If Golden State wins"})
    put(Venue.POLYMARKET, "P-WNBA", "WNBA Golden State vs Toronto winner today",
        {"outcomes": _json.dumps(["Valkyries", "Toronto"])})

    engine = FakeEngine(equivalent_ids={"K-UFC", "K-WNBA"})  # LLM: both equivalent
    loop = PairingLoop(
        markets_repo=markets, engine=engine, registry_repo=registry,
        audit_repo=audit_repo, publish=loader.publish, max_llm_calls=10,
    )
    await loop.run_once()
    entries = {e.kalshi_ticker: e for e in registry.all_entries()}
    # UFC: LLM equivalent + resolver override -> VERIFIED with INVERTED polarity
    # (NOT the DIRECT the FakeEngine returned)
    assert entries["K-UFC"].status is PairStatus.VERIFIED
    assert entries["K-UFC"].outcome_polarity is OutcomePolarity.INVERTED
    # WNBA: ambiguous polarity -> rejected deterministically, LLM NOT consulted
    assert entries["K-WNBA"].status is PairStatus.NOT_EQUIVALENT
    assert engine.calls == 1  # only the UFC pair reached the LLM


async def test_polarity_audit_corrects_wrong_verified_polarity(tmp_path):
    """A versus pair verified (wrongly) DIRECT — e.g. before its outcomes were in
    the catalog — is corrected to the resolver's INVERTED by the re-audit."""
    import json as _json

    from stellasaurus.hot_path.snapshot import PairRegistryEntry
    from stellasaurus.storage.markets_repo import MarketRow
    from stellasaurus.venues.base import market_fingerprint
    registry, markets, audit_repo, loader, store = _fixture(tmp_path)

    def put(venue, nid, title, raw):
        m = RawMarket(venue, nid, title, title, "src", T0, "open", raw)
        markets.upsert(MarketRow(
            venue=venue, native_id=nid, title=title, rules_text=title,
            settlement_source="src", resolves_at_ms=T0, status="open",
            terms_fingerprint=market_fingerprint(m), raw_json=_json.dumps(raw)))

    put(Venue.KALSHI, "K-BLG", "Bilibili Gaming vs Hanwha",
        {"yes_sub_title": "Bilibili Gaming", "rules_primary": "If Bilibili Gaming wins"})
    put(Venue.POLYMARKET, "P-BLG", "Bilibili vs Hanwha",
        {"outcomes": _json.dumps(["Bilibili Gaming", "Hanwha Life Esports"])})
    # seed the registry with the WRONG polarity (resolver would say DIRECT)
    registry.upsert(PairRegistryEntry(
        "llm-k-blg--p-blg", "prop", "K-BLG", "P-BLG", OutcomePolarity.INVERTED,
        PairStatus.VERIFIED, T0, None, 0, "fp", PairSource.LLM))

    loop = PairingLoop(
        markets_repo=markets, engine=FakeEngine(set()), registry_repo=registry,
        audit_repo=audit_repo, publish=loader.publish, max_llm_calls=10,
    )
    corrected = loop.audit_polarity()
    assert corrected == 1
    entry = {e.kalshi_ticker: e for e in registry.all_entries()}["K-BLG"]
    assert entry.outcome_polarity is OutcomePolarity.DIRECT  # fixed
