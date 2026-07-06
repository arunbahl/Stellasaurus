"""Structured matchers: weather bracket determinism + entity matching."""

from stellasaurus.background.matchers import (
    EntityMatcher,
    WeatherTempMatcher,
    run_matchers,
)
from stellasaurus.common.types import OutcomePolarity, PairStatus, Venue
from stellasaurus.venues.base import RawMarket

T0 = 1_999_000_000_000


def _k_temp(nid: str, rules: str) -> RawMarket:
    return RawMarket(Venue.KALSHI, nid, "High temp?", rules, "NWS", T0, "open", {})


def _p_temp(nid: str, desc: str) -> RawMarket:
    return RawMarket(Venue.POLYMARKET, nid, "Highest temperature?", desc, "NWS", T0, "open", {})


K_RULES = (
    "If the highest temperature recorded at Miami International Airport for "
    "July 06, 2026 as reported by the National Weather Service's Climatological "
    "Report (Daily), is {}, then the market resolves to Yes."
)
P_DESC = (
    "Will the highest temperature recorded at Miami International Airport (KMIA) "
    "for 2026-07-06 as reported by the National Weather Service's Climatological "
    "Report (Daily) be {}?"
)


def test_weather_bracket_exact_match_is_verified():
    k = _k_temp("KXHIGHMIA-26JUL06-B93.5", K_RULES.format("between 93-94°"))
    p = _p_temp("tc-temp-miahigh-2026-07-06-gte93lt94f", P_DESC.format("between 93F and 94F"))
    out = WeatherTempMatcher().match([k], [p])
    assert len(out) == 1
    c = out[0]
    assert c.preverdict is PairStatus.VERIFIED
    assert c.polarity is OutcomePolarity.DIRECT
    assert c.matched_fields is not None
    assert c.matched_fields["date"] == "2026-07-06"
    assert c.matched_fields["kalshi_range"] == [93, 94]
    assert c.matched_fields["poly_range"] == [93, 94]
    assert "miami" in c.matched_fields["station"]


def test_weather_open_ended_ranges_normalize():
    # Kalshi "less than 87" == <=86 ; Poly "less than or equal to 86F"
    k = _k_temp("KXHIGHMIA-26JUL06-T87", K_RULES.format("less than 87°"))
    p = _p_temp("tc-temp-miahigh-2026-07-06-lt87f", P_DESC.format("less than or equal to 86F"))
    out = WeatherTempMatcher().match([k], [p])
    assert out and out[0].preverdict is PairStatus.VERIFIED
    # Kalshi "greater than 94" == >=95 ; Poly "greater than or equal to 95F"
    k2 = _k_temp("KXHIGHMIA-26JUL06-T94", K_RULES.format("greater than 94°"))
    p2 = _p_temp("tc-temp-miahigh-2026-07-06-gte95f", P_DESC.format("greater than or equal to 95F"))
    out2 = WeatherTempMatcher().match([k2], [p2])
    assert out2 and out2[0].preverdict is PairStatus.VERIFIED


def test_weather_different_station_never_matches():
    ny_rules = K_RULES.format("between 93-94°").replace(
        "Miami International Airport", "Central Park, New York"
    )
    k = _k_temp("KXHIGHNY-26JUL06-B93.5", ny_rules)
    p = _p_temp("tc-temp-miahigh-2026-07-06-gte93lt94f", P_DESC.format("between 93F and 94F"))
    assert WeatherTempMatcher().match([k], [p]) == []


def test_weather_different_bracket_produces_nothing():
    k = _k_temp("KXHIGHMIA-26JUL06-B91.5", K_RULES.format("between 91-92°"))
    p = _p_temp("tc-temp-miahigh-2026-07-06-gte93lt94f", P_DESC.format("between 93F and 94F"))
    out = WeatherTempMatcher().match([k], [p])
    assert out == []  # different brackets: not a candidate at all


def test_weather_unparseable_range_falls_back_to_llm_candidate():
    k = _k_temp("KXHIGHMIA-26JUL06-B93.5", K_RULES.format("in the 93 to 94 degree band"))
    p = _p_temp("tc-temp-miahigh-2026-07-06-gte93lt94f", P_DESC.format("between 93F and 94F"))
    out = WeatherTempMatcher().match([k], [p])
    assert len(out) == 1 and out[0].preverdict is None  # needs the LLM


def test_entity_matcher_shares_player_names():
    k = RawMarket(Venue.KALSHI, "KXWCGOAL-X", "Will Ibrahim Sabra score in AUT vs JOR?",
                  "If Ibrahim Sabra scores a goal...", None, T0, "open", {})
    p = RawMarket(
        Venue.POLYMARKET, "astatc-fwc-x",
        "Will Ibrahim Sabra record at least 1 goals in AUT vs JOR?",
        "Resolves Yes if Ibrahim Sabra scores 1+ goals", None, T0, "open", {},
    )
    out = EntityMatcher().match([k], [p])
    assert len(out) == 1 and out[0].preverdict is None


def test_entity_matcher_rejects_no_shared_entities():
    k = RawMarket(
        Venue.KALSHI, "K1", "Will Lionel Messi score tonight?", "", None, T0, "open", {}
    )
    p = RawMarket(
        Venue.POLYMARKET, "P1", "Will Erling Haaland score tonight?", "", None, T0, "open", {}
    )
    assert EntityMatcher().match([k], [p]) == []


def test_run_matchers_prefers_deterministic():
    k = _k_temp("KXHIGHMIA-26JUL06-B93.5", K_RULES.format("between 93-94°"))
    p = _p_temp("tc-temp-miahigh-2026-07-06-gte93lt94f", P_DESC.format("between 93F and 94F"))
    out = run_matchers([k], [p])
    assert len(out) == 1 and out[0].preverdict is PairStatus.VERIFIED
