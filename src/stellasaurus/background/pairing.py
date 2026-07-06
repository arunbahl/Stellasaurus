"""Candidate generation + pairing loop (DESIGN §6.1 / §6.2 step 1 + 3).

Cheap, high-recall candidate generation across the two catalogs, a deterministic
pre-check, then LLM acceptance-criteria evaluation for whatever survives — the
verdicts are written to ``pair_registry`` with ``source=LLM`` and a fresh
registry snapshot is published.

Candidate strategy (deliberately simple for v1):
  * bucket both venues' markets by UTC resolution date (±1 day tolerance),
  * score title-token overlap (Jaccard) within a bucket,
  * REQUIRE numeric tokens to match exactly when both titles contain numbers
    (thresholds/dates differing is the classic false-candidate),
  * keep the best-scoring Polymarket market per Kalshi market above a floor.

LLM spend is bounded by ``max_llm_calls`` per cycle; already-evaluated leg pairs
(any status) are skipped, so the loop converges instead of re-billing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from stellasaurus.background.equivalence import EquivalenceEngine, contract_from_market
from stellasaurus.common.clock import wall_ms
from stellasaurus.common.ids import normalize_text, slugify, terms_fingerprint
from stellasaurus.common.logging import audit, get_logger
from stellasaurus.common.types import PairSource, Venue
from stellasaurus.hot_path.snapshot import PairRegistryEntry
from stellasaurus.storage.audit_repo import AuditRepo
from stellasaurus.storage.registry_repo import RegistryRepo
from stellasaurus.venues.base import RawMarket, VenueClient, market_fingerprint

_log = get_logger("background.pairing")

_DAY_MS = 86_400_000
_STOPWORDS = frozenset(
    "the a an of on in at for to by be will is are was and "
    "or vs versus with market resolves resolve yes no if than does do".split()
)
_NUM = re.compile(r"\d+(?:\.\d+)?")


@dataclass(frozen=True, slots=True)
class CandidatePair:
    kalshi: RawMarket
    poly: RawMarket
    score: float


def _tokens(market: RawMarket) -> frozenset[str]:
    text = normalize_text(f"{market.title} {market.rules_text or ''}")
    return frozenset(t for t in re.split(r"[^a-z0-9.]+", text) if t and t not in _STOPWORDS)


def _numbers(tokens: frozenset[str]) -> frozenset[str]:
    return frozenset(t for t in tokens if _NUM.fullmatch(t))


def generate_candidates(
    kalshi_markets: list[RawMarket],
    poly_markets: list[RawMarket],
    *,
    min_score: float = 0.35,
) -> list[CandidatePair]:
    """High-recall candidates: same resolution day (±1), best token overlap."""
    by_day: dict[int, list[tuple[RawMarket, frozenset[str]]]] = {}
    for p in poly_markets:
        if p.resolves_at_ms is None:
            continue
        by_day.setdefault(p.resolves_at_ms // _DAY_MS, []).append((p, _tokens(p)))

    out: list[CandidatePair] = []
    for k in kalshi_markets:
        if k.resolves_at_ms is None:
            continue
        k_tokens = _tokens(k)
        if not k_tokens:
            continue
        k_nums = _numbers(k_tokens)
        day = k.resolves_at_ms // _DAY_MS
        best: CandidatePair | None = None
        for d in (day - 1, day, day + 1):
            for p, p_tokens in by_day.get(d, []):
                union = k_tokens | p_tokens
                if not union:
                    continue
                score = len(k_tokens & p_tokens) / len(union)
                if score < min_score:
                    continue
                # Numeric tokens (thresholds, dates) must agree when both exist:
                # "temp > 84" vs "temp > 86" overlap heavily but never match.
                p_nums = _numbers(p_tokens)
                if k_nums and p_nums and not (k_nums & p_nums):
                    continue
                if best is None or score > best.score:
                    best = CandidatePair(kalshi=k, poly=p, score=score)
        if best is not None:
            out.append(best)
    out.sort(key=lambda c: c.score, reverse=True)
    _log.info("candidates_generated", kalshi=len(kalshi_markets), poly=len(poly_markets),
              candidates=len(out))
    return out


class PairingLoop:
    """One background cycle: catalogs -> candidates -> LLM -> registry rows."""

    def __init__(
        self,
        *,
        clients: dict[Venue, VenueClient],
        engine: EquivalenceEngine,
        registry_repo: RegistryRepo,
        audit_repo: AuditRepo,
        publish: object,  # callable () -> RegistrySnapshot (RegistryLoader.publish)
        max_llm_calls: int = 10,
        min_score: float = 0.35,
    ) -> None:
        self._clients = clients
        self._engine = engine
        self._registry = registry_repo
        self._audit = audit_repo
        self._publish = publish
        self._max_llm_calls = max_llm_calls
        self._min_score = min_score

    async def run_once(self) -> int:
        """Returns the number of LLM evaluations performed this cycle."""
        if not self._engine.configured:
            _log.warning("pairing_skipped", reason="llm_not_configured")
            return 0
        kalshi = await self._clients[Venue.KALSHI].list_markets()
        poly = await self._clients[Venue.POLYMARKET].list_markets()
        candidates = generate_candidates(kalshi, poly, min_score=self._min_score)

        evaluated = 0
        for cand in candidates:
            if evaluated >= self._max_llm_calls:
                break
            if self._registry.find_by_legs(cand.kalshi.native_id, cand.poly.native_id):
                continue  # already judged (any status); re-eval only on terms change
            try:
                verdict = await self._engine.evaluate(
                    contract_from_market(cand.kalshi), contract_from_market(cand.poly)
                )
            except Exception as exc:  # noqa: BLE001 - one bad eval must not kill the cycle
                _log.warning("pairing_eval_failed", kalshi=cand.kalshi.native_id,
                             poly=cand.poly.native_id, error=str(exc))
                continue
            evaluated += 1
            status, polarity, criteria = EquivalenceEngine.disposition(verdict)
            resolves = [m.resolves_at_ms for m in (cand.kalshi, cand.poly) if m.resolves_at_ms]
            entry = PairRegistryEntry(
                pair_id=f"llm-{slugify(cand.kalshi.native_id)}--{slugify(cand.poly.native_id)}",
                canonical_proposition=cand.kalshi.title,
                kalshi_ticker=cand.kalshi.native_id,
                poly_market_slug=cand.poly.native_id,
                outcome_polarity=polarity,
                status=status,
                resolves_at_ms=max(resolves) if resolves else None,
                acceptance_criteria=criteria,
                last_verified_at_ms=wall_ms(),
                terms_fingerprint=terms_fingerprint(
                    {"kalshi": market_fingerprint(cand.kalshi),
                     "poly": market_fingerprint(cand.poly)}
                ),
                source=PairSource.LLM,
            )
            self._registry.upsert(entry)
            audit(
                self._audit,
                actor="pairing_loop",
                event_type="PAIR_EVALUATED",
                pair_id=entry.pair_id,
                status=status.value,
                polarity=polarity.value,
                score=round(cand.score, 3),
                reason=criteria.get("reason", ""),
            )
        if evaluated:
            self._publish()  # type: ignore[operator]
        _log.info("pairing_cycle_done", candidates=len(candidates), evaluated=evaluated)
        return evaluated
