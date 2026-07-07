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

import asyncio
import json
import re
from dataclasses import dataclass

from stellasaurus.background.equivalence import EquivalenceEngine, contract_from_market
from stellasaurus.background.matchers import (
    MatchedCandidate,
    _poly_versus_outcomes,
    resolve_versus_polarity,
    run_matchers,
)
from stellasaurus.baml_client.types import EquivalenceVerdict
from stellasaurus.common.clock import wall_ms
from stellasaurus.common.ids import normalize_text, slugify, terms_fingerprint
from stellasaurus.common.logging import audit, get_logger
from stellasaurus.common.types import OutcomePolarity, PairSource, PairStatus, Venue
from stellasaurus.hot_path.snapshot import PairRegistryEntry
from stellasaurus.storage.audit_repo import AuditRepo
from stellasaurus.storage.markets_repo import MarketRow, MarketsRepo
from stellasaurus.storage.registry_repo import RegistryRepo
from stellasaurus.venues.base import RawMarket, market_fingerprint

_log = get_logger("background.pairing")

_DAY_MS = 86_400_000
_STOPWORD_TEXT = (
    "the a an of on in at for to by be will is are was and or vs versus "
    "with market resolves resolve yes no if than does do"
)
_STOPWORDS = frozenset(_STOPWORD_TEXT.split())
_NUM = re.compile(r"\d+(?:\.\d+)?")


def _row_to_raw(m: MarketRow) -> RawMarket:
    # Load the stored venue fields back into .raw so structured matchers can read
    # them (e.g. Polymarket `outcomes` / Kalshi `yes_sub_title` for versus
    # polarity). Empty when the market predates raw_json persistence.
    raw: dict[str, object] = {}
    if m.raw_json:
        try:
            raw = json.loads(m.raw_json)
        except json.JSONDecodeError:
            raw = {}
    return RawMarket(
        venue=m.venue, native_id=m.native_id, title=m.title, rules_text=m.rules_text,
        settlement_source=m.settlement_source, resolves_at_ms=m.resolves_at_ms,
        status=m.status, raw=raw,
    )


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
        markets_repo: MarketsRepo,
        engine: EquivalenceEngine,
        registry_repo: RegistryRepo,
        audit_repo: AuditRepo,
        publish: object,  # callable () -> RegistrySnapshot (RegistryLoader.publish)
        max_llm_calls: int = 10,
        min_score: float = 0.35,
        llm_concurrency: int = 8,
    ) -> None:
        self._markets = markets_repo
        self._engine = engine
        self._registry = registry_repo
        self._audit = audit_repo
        self._publish = publish
        self._max_llm_calls = max_llm_calls
        self._min_score = min_score
        self._llm_concurrency = max(1, llm_concurrency)

    def _write_entry(
        self,
        *,
        kalshi: RawMarket,
        poly: RawMarket,
        status: PairStatus,
        polarity: OutcomePolarity,
        criteria: dict[str, object],
        source: PairSource,
        score: float,
    ) -> None:
        resolves = [m.resolves_at_ms for m in (kalshi, poly) if m.resolves_at_ms]
        prefix = "det" if source is PairSource.STRUCTURED else "llm"
        entry = PairRegistryEntry(
            pair_id=f"{prefix}-{slugify(kalshi.native_id)}--{slugify(poly.native_id)}",
            canonical_proposition=kalshi.title,
            kalshi_ticker=kalshi.native_id,
            poly_market_slug=poly.native_id,
            outcome_polarity=polarity,
            status=status,
            resolves_at_ms=max(resolves) if resolves else None,
            acceptance_criteria=criteria,
            last_verified_at_ms=wall_ms(),
            terms_fingerprint=terms_fingerprint(
                {"kalshi": market_fingerprint(kalshi), "poly": market_fingerprint(poly)}
            ),
            source=source,
        )
        self._registry.upsert(entry)
        audit(
            self._audit,
            actor="pairing_loop",
            event_type="PAIR_EVALUATED",
            pair_id=entry.pair_id,
            status=status.value,
            polarity=polarity.value,
            source=source.value,
            score=round(score, 3),
            reason=str(criteria.get("reason", "")),
        )

    async def run_once(self, llm_budget: int | None = None) -> int:
        """Returns the number of LLM evaluations performed this cycle.
        ``llm_budget=0`` runs a STRUCTURED-ONLY pass (deterministic matchers
        still write verdicts; nothing is spent on the LLM) — used by the fast
        near-resolution priority cycle.

        Candidates come from the ACCUMULATED catalog (the markets table filled by
        the rotating series sweeps) — not a single fetch — so every category ever
        swept participates, and coverage grows as the rotation progresses.
        """
        now = wall_ms()
        kalshi_rows = self._markets.unresolved_markets(Venue.KALSHI, now_ms=now)
        poly_rows = self._markets.unresolved_markets(Venue.POLYMARKET, now_ms=now)
        kalshi = [_row_to_raw(m) for m in kalshi_rows]
        poly = [_row_to_raw(m) for m in poly_rows]

        # 1) Structured matchers first (DESIGN §6.2 step 1): deterministic
        #    verdicts are written immediately with source=STRUCTURED — no LLM.
        structured = run_matchers(kalshi, poly)
        wrote = 0
        llm_queue: list[MatchedCandidate] = []
        matched_legs: set[tuple[str, str]] = set()
        for c in structured:
            matched_legs.add((c.kalshi.native_id, c.poly.native_id))
            if self._registry.find_by_legs(c.kalshi.native_id, c.poly.native_id):
                continue
            if c.preverdict is not None:
                self._write_entry(
                    kalshi=c.kalshi, poly=c.poly, status=c.preverdict,
                    polarity=c.polarity,
                    criteria={"reason": f"structured:{c.strategy}",
                              "matched_fields": c.matched_fields or {}},
                    source=PairSource.STRUCTURED, score=c.score,
                )
                wrote += 1
            else:
                llm_queue.append(c)

        # 2) Generic token candidates for anything the matchers didn't cover.
        for g in generate_candidates(kalshi, poly, min_score=self._min_score):
            key = (g.kalshi.native_id, g.poly.native_id)
            if key not in matched_legs:
                llm_queue.append(
                    MatchedCandidate(g.kalshi, g.poly, g.score, "token")
                )

        # 3) Versus polarity resolution (deterministic) applied to ALL LLM
        #    candidates regardless of how they were generated: a two-outcome
        #    "versus" market whose polarity we CAN'T resolve is REJECTED here (no
        #    LLM, never guess which side YES pays on); the rest keep the resolver
        #    verdict to override the LLM's polarity below.
        versus_hint: dict[tuple[str, str], OutcomePolarity] = {}
        remaining: list[MatchedCandidate] = []
        for c in llm_queue:
            legs = (c.kalshi.native_id, c.poly.native_id)
            if _poly_versus_outcomes(c.poly) is None:
                remaining.append(c)
                continue
            hint = resolve_versus_polarity(c.kalshi, c.poly)
            if hint is None:
                if not self._registry.find_by_legs(*legs):
                    self._write_entry(
                        kalshi=c.kalshi, poly=c.poly, status=PairStatus.NOT_EQUIVALENT,
                        polarity=OutcomePolarity.DIRECT,
                        criteria={"reason": "versus_polarity_ambiguous"},
                        source=PairSource.STRUCTURED, score=c.score,
                    )
                    wrote += 1
            else:
                versus_hint[legs] = hint
                remaining.append(c)
        llm_queue = remaining

        # 4) LLM evaluation for the remainder, budget-capped.
        budget = self._max_llm_calls if llm_budget is None else llm_budget
        evaluated = 0
        if budget == 0:
            pass  # structured-only pass
        elif not self._engine.configured and llm_queue:
            _log.warning("pairing_llm_skipped", queued=len(llm_queue),
                         reason="llm_not_configured")
        elif self._engine.configured:
            # ``budget`` caps LLM CALLS this cycle. Evaluate up to that many
            # candidates concurrently (network-bound; venue-independent), then
            # write verdicts sequentially — SQLite writes stay single-threaded.
            pending = [
                c for c in llm_queue
                if not self._registry.find_by_legs(c.kalshi.native_id, c.poly.native_id)
            ][:budget]
            sem = asyncio.Semaphore(self._llm_concurrency)

            async def _eval(
                c: MatchedCandidate,
            ) -> tuple[MatchedCandidate, EquivalenceVerdict] | None:
                async with sem:
                    try:
                        v = await self._engine.evaluate(
                            contract_from_market(c.kalshi), contract_from_market(c.poly)
                        )
                        return c, v
                    except Exception as exc:  # noqa: BLE001 - one flake must not kill the cycle
                        _log.warning("pairing_eval_failed", kalshi=c.kalshi.native_id,
                                     poly=c.poly.native_id, error=str(exc))
                        return None

            # Write each verdict AS IT COMPLETES (not after the whole gather), so
            # a long cycle streams results and an interruption never discards
            # finished evaluations — they're already durable and skipped next run.
            tasks = [asyncio.create_task(_eval(c)) for c in pending]
            for fut in asyncio.as_completed(tasks):
                res = await fut
                if res is None:
                    continue
                c, verdict = res
                evaluated += 1
                status, polarity, criteria = EquivalenceEngine.disposition(verdict)
                # For versus markets, TRUST the LLM on equivalence (same event +
                # proposition) but OVERRIDE its polarity with the deterministic
                # resolver — the LLM reliably mislabels which token is YES.
                hint = versus_hint.get((c.kalshi.native_id, c.poly.native_id))
                if hint is not None:
                    polarity = hint
                    criteria = {**criteria, "polarity_source": "versus_resolver"}
                self._write_entry(
                    kalshi=c.kalshi, poly=c.poly, status=status, polarity=polarity,
                    criteria=criteria, source=PairSource.LLM, score=c.score,
                )
                wrote += 1

        if wrote:
            self._publish()  # type: ignore[operator]
        _log.info("pairing_cycle_done", structured=len(structured),
                  llm_queued=len(llm_queue), evaluated=evaluated, wrote=wrote)
        return evaluated
