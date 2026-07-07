"""Structured per-family cross-venue matchers (DESIGN §6.1 + §6.2 step 1).

Matchers produce ``MatchedCandidate``s. A matcher that can settle equivalence
deterministically attaches a ``preverdict`` (VERIFIED / NOT_EQUIVALENT) with the
matched fields recorded — those bypass the LLM entirely. Candidates without a
preverdict go to the LLM as usual.

v1 matchers:
  * ``WeatherTempMatcher`` — daily high-temperature brackets. Fully structured on
    both venues (station, date, integer °F range), so verdicts are deterministic.
    Ranges are parsed from the RULES/DESCRIPTION text only (never from slugs or
    tickers, whose grammars proved misleading), and unparseable text falls back
    to an LLM candidate rather than a guess.
  * ``EntityMatcher`` — generic sports/props fallback: shared proper-name
    entities (players, teams) + same resolution day (±1) + agreeing numeric
    tokens. Never deterministic; always defers to the LLM.
"""

from __future__ import annotations

import calendar
import json
import re
from dataclasses import dataclass, field
from typing import Any

from stellasaurus.common.ids import normalize_text
from stellasaurus.common.logging import get_logger
from stellasaurus.common.types import OutcomePolarity, PairStatus
from stellasaurus.venues.base import RawMarket

_log = get_logger("background.matchers")


@dataclass(frozen=True, slots=True)
class MatchedCandidate:
    kalshi: RawMarket
    poly: RawMarket
    score: float
    strategy: str
    preverdict: PairStatus | None = None  # None -> needs the LLM
    polarity: OutcomePolarity = OutcomePolarity.DIRECT
    matched_fields: dict[str, Any] | None = field(default=None)


# --------------------------------------------------------------------------
# Weather: daily high-temperature brackets
# --------------------------------------------------------------------------

# Station identification is EXTRACTED from the rules text (no curated city
# list — new cities/stations work automatically). Both venues phrase it as
# "...recorded at/in <station phrase> for <date>...". Two markets share a
# station when one phrase's token set contains the other's (e.g. Kalshi
# "San Francisco" ⊆ Poly "San Francisco International Airport (KSFO)").
_STATION_PHRASE = re.compile(r"recorded (?:at|in) (.+?) for ")
_STATION_STOP = frozenset({"in", "the", "at", "city"})

# Range = closed integer interval [lo, hi]; None = open end.
Range = tuple[int | None, int | None]

# Kalshi rules_primary phrasings.
_K_BETWEEN = re.compile(r"between (\d+)\s*-\s*(\d+)")
_K_GREATER = re.compile(r"greater than (\d+)")
_K_LESS = re.compile(r"less than (\d+)")
# Polymarket description phrasings.
_P_BETWEEN = re.compile(r"between (\d+)\s*f and (\d+)\s*f")
_P_LE = re.compile(r"less than or equal to (\d+)\s*f")
_P_GE = re.compile(r"greater than or equal to (\d+)\s*f")
_DATE_ISO = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
# Venues abbreviate inconsistently ("July 06, 2026" vs "jul 6, 2026"): match the
# 3-letter month prefix with an optional tail.
_DATE_LONG = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2}),?\s+(\d{4})"
)
_MONTHS = {m.lower()[:3]: i for i, m in enumerate(calendar.month_name) if m}


def _station_tokens(text: str) -> frozenset[str] | None:
    """Token set of the station phrase, or None if no phrase is present.

    Tokens of <=2 chars are dropped: state codes ("CA", "NY") appear on one
    venue but not the other and would break the subset comparison, and station
    call signs are 3-4 chars so they survive.
    """
    m = _STATION_PHRASE.search(text)
    if not m:
        return None
    tokens = frozenset(
        t for t in re.split(r"[^a-z0-9]+", m.group(1))
        if len(t) > 2 and t not in _STATION_STOP
    )
    return tokens or None


def _same_station(a: frozenset[str], b: frozenset[str]) -> bool:
    return a <= b or b <= a


def _date_key(text: str) -> str | None:
    m = _DATE_ISO.search(text)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = _DATE_LONG.search(text)
    if m:
        return f"{m.group(3)}-{_MONTHS[m.group(1)]:02d}-{int(m.group(2)):02d}"
    return None


def _kalshi_range(text: str) -> Range | None:
    if m := _K_BETWEEN.search(text):
        return (int(m.group(1)), int(m.group(2)))
    if m := _K_GREATER.search(text):
        return (int(m.group(1)) + 1, None)  # integer reporting: >94 == >=95
    if m := _K_LESS.search(text):
        return (None, int(m.group(1)) - 1)  # <87 == <=86
    return None


def _poly_range(text: str) -> Range | None:
    if m := _P_BETWEEN.search(text):
        return (int(m.group(1)), int(m.group(2)))
    if m := _P_LE.search(text):
        return (None, int(m.group(1)))
    if m := _P_GE.search(text):
        return (int(m.group(1)), None)
    return None


class WeatherTempMatcher:
    """Deterministic matcher for daily-high-temperature bracket markets."""

    strategy = "weather_temp"

    def applies_kalshi(self, m: RawMarket) -> bool:
        return m.native_id.startswith("KXHIGH")

    def applies_poly(self, m: RawMarket) -> bool:
        return m.native_id.startswith("tc-temp-")

    def match(
        self, kalshi: list[RawMarket], poly: list[RawMarket]
    ) -> list[MatchedCandidate]:
        # index poly by date; station phrases compared pairwise (token subset)
        PEntry = tuple[RawMarket, frozenset[str], Range | None]
        p_index: dict[str, list[PEntry]] = {}
        for p in poly:
            if not self.applies_poly(p):
                continue
            text = normalize_text(f"{p.title} {p.rules_text or ''}")
            st, dt_ = _station_tokens(text), _date_key(f"{p.native_id} {text}")
            if st is None or dt_ is None:
                continue
            p_index.setdefault(dt_, []).append((p, st, _poly_range(text)))

        out: list[MatchedCandidate] = []
        for k in kalshi:
            if not self.applies_kalshi(k):
                continue
            text = normalize_text(f"{k.title} {k.rules_text or ''}")
            st, dt_ = _station_tokens(text), _date_key(text)
            if st is None or dt_ is None:
                continue
            k_range = _kalshi_range(text)
            for p, p_st, p_range in p_index.get(dt_, []):
                if not _same_station(st, p_st):
                    continue
                if k_range is None or p_range is None:
                    # same station+date but unparseable range -> let the LLM judge
                    out.append(MatchedCandidate(k, p, 0.9, self.strategy))
                    continue
                if k_range == p_range:
                    out.append(MatchedCandidate(
                        k, p, 1.0, self.strategy,
                        preverdict=PairStatus.VERIFIED,
                        polarity=OutcomePolarity.DIRECT,
                        matched_fields={
                            "station": sorted(st | p_st), "date": dt_,
                            "kalshi_range": list(k_range), "poly_range": list(p_range),
                        },
                    ))
                # Different ranges on the same station/date are simply different
                # brackets — not worth recording as NOT_EQUIVALENT rows.
        return out


# --------------------------------------------------------------------------
# Generic entity matcher (sports games / player props) — LLM candidates only
# --------------------------------------------------------------------------

_ENTITY = re.compile(r"[A-Z][a-zA-Z'’.]+(?:\s+[A-Z][a-zA-Z'’.]+)+")  # multi-word proper names
_CODE = re.compile(r"\b[A-Z]{2,4}\b")  # team/country codes (AUT, JOR, LAD)
_NUM_TOKEN = re.compile(r"\d+(?:\.\d+)?")
_DAY_MS = 86_400_000


def _entities(m: RawMarket) -> tuple[frozenset[str], frozenset[str], frozenset[str]]:
    text = f"{m.title} {m.rules_text or ''}"
    names = frozenset(x.lower() for x in _ENTITY.findall(text))
    codes = frozenset(_CODE.findall(text))
    nums = frozenset(_NUM_TOKEN.findall(m.title))
    return names, codes, nums


# --- Deterministic polarity for two-outcome "versus" markets ---------------
# CRITICAL (validated live 2026-07-06): Polymarket's book is the LONG side of
# outcomes[0]; the LLM cannot reliably infer from titles which of two teams a
# YES pays on, so it mislabeled EVERY sports pair DIRECT — inverting the hedge
# into a 2x directional bet and manufacturing phantom "edges" of 24-38c. Resolve
# polarity structurally instead: match the Kalshi-YES entity to exactly one of
# Polymarket's two named outcomes.
_WORD = re.compile(r"[a-z0-9]+")
# Generic connective/role noise dropped before entity token matching (NOT a
# category allowlist — just words that never discriminate two named sides).
_VS_NOISE = frozenset({"esports", "the", "fc", "team", "ev", "of", "and", "united"})


def _tokset(text: str) -> frozenset[str]:
    return frozenset(t for t in _WORD.findall((text or "").lower()) if t not in _VS_NOISE)


def _poly_versus_outcomes(m: RawMarket) -> tuple[str, str] | None:
    """(outcome0, outcome1) if the Poly market has two NAMED outcomes; else None.
    Poly returns ``outcomes`` as a JSON string, and Yes/No markets are not versus."""
    raw = m.raw.get("outcomes")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return None
    if not (isinstance(raw, list) and len(raw) == 2):
        return None
    a, b = str(raw[0]), str(raw[1])
    if {a.lower(), b.lower()} <= {"yes", "no"}:
        return None
    return a, b


def _kalshi_yes_tokens(m: RawMarket) -> frozenset[str]:
    """Tokens naming the entity Kalshi's YES resolves to."""
    ys = m.raw.get("yes_sub_title") or m.raw.get("subtitle")
    if isinstance(ys, str) and ys.strip():
        return _tokset(ys)
    rp = m.raw.get("rules_primary")
    rules = m.rules_text or (rp if isinstance(rp, str) else "")
    match = re.search(r"[Ii]f (.+?) wins", rules)
    return _tokset(match.group(1)) if match else frozenset()


def resolve_versus_polarity(kalshi: RawMarket, poly: RawMarket) -> OutcomePolarity | None:
    """DIRECT/INVERTED when the Kalshi-YES entity matches exactly ONE Poly
    outcome; None when the market isn't a versus market or the match is
    ambiguous (leave those unverified rather than guess)."""
    outcomes = _poly_versus_outcomes(poly)
    if outcomes is None:
        return None
    k_yes = _kalshi_yes_tokens(kalshi)
    if not k_yes:
        return None
    o0, o1 = _tokset(outcomes[0]), _tokset(outcomes[1])
    d0, d1 = o0 - o1, o1 - o0  # discriminating tokens only
    m0, m1 = bool(k_yes & d0), bool(k_yes & d1)
    if m0 and not m1:
        return OutcomePolarity.DIRECT     # Poly-YES(outcomes[0]) == Kalshi-YES
    if m1 and not m0:
        return OutcomePolarity.INVERTED   # Poly-YES is the OTHER side
    return None


class EntityMatcher:
    """Shared proper-name entities + same day (±1) + agreeing title numbers."""

    strategy = "entity"

    def match(
        self, kalshi: list[RawMarket], poly: list[RawMarket]
    ) -> list[MatchedCandidate]:
        Entry = tuple[RawMarket, frozenset[str], frozenset[str], frozenset[str]]
        p_by_day: dict[int, list[Entry]] = {}
        for p in poly:
            if p.resolves_at_ms is None:
                continue
            names, codes, nums = _entities(p)
            if not names and not codes:
                continue
            p_by_day.setdefault(p.resolves_at_ms // _DAY_MS, []).append((p, names, codes, nums))

        out: list[MatchedCandidate] = []
        for k in kalshi:
            if k.resolves_at_ms is None:
                continue
            k_names, k_codes, k_nums = _entities(k)
            if not k_names and not k_codes:
                continue
            day = k.resolves_at_ms // _DAY_MS
            best: MatchedCandidate | None = None
            for d in (day - 1, day, day + 1):
                for p, p_names, p_codes, p_nums in p_by_day.get(d, []):
                    shared_names = k_names & p_names
                    shared_codes = k_codes & p_codes
                    if not shared_names and len(shared_codes) < 2:
                        continue
                    if k_nums and p_nums and not (k_nums & p_nums):
                        continue
                    denom = max(len(k_names | p_names), 1)
                    score = 0.5 + 0.5 * len(shared_names) / denom
                    if best is None or score > best.score:
                        best = MatchedCandidate(k, p, score, self.strategy)
            if best is not None:
                # For a two-outcome "versus" market, resolve polarity
                # deterministically and VERIFY it here — bypassing the LLM, which
                # cannot reliably tell which team Poly's YES pays on. Ambiguous
                # ones fall through to the LLM as before (preverdict stays None).
                pol = resolve_versus_polarity(best.kalshi, best.poly)
                if pol is not None:
                    best = MatchedCandidate(
                        best.kalshi, best.poly, best.score, "versus",
                        preverdict=PairStatus.VERIFIED, polarity=pol,
                    )
                out.append(best)
        return out


def run_matchers(
    kalshi: list[RawMarket], poly: list[RawMarket]
) -> list[MatchedCandidate]:
    """Run all structured matchers; dedupe by leg pair, keep the best score."""
    best: dict[tuple[str, str], MatchedCandidate] = {}
    for matcher in (WeatherTempMatcher(), EntityMatcher()):
        for c in matcher.match(kalshi, poly):
            key = (c.kalshi.native_id, c.poly.native_id)
            prior = best.get(key)
            # deterministic verdicts always win; otherwise higher score wins
            if prior is None or (c.preverdict and not prior.preverdict) \
               or (bool(c.preverdict) == bool(prior.preverdict) and c.score > prior.score):
                best[key] = c
    out = sorted(best.values(), key=lambda c: (c.preverdict is None, -c.score))
    _log.info("matchers_ran", candidates=len(out),
              deterministic=sum(1 for c in out if c.preverdict is not None))
    return out
