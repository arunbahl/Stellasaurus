// Resolution-equivalence evaluation (DESIGN §6.2), simplified schema.
//
// The model still CHECKS the four acceptance-criteria dimensions (proposition,
// settlement source, timing/cutoff, edge cases) — but reports only the verdict,
// the polarity, and a short reason. The heavyweight per-dimension extraction
// schema proved harder for models to emit reliably than the judgment itself.

// Typed input: one contract's catalog fields.
class Contract {
  native_id string
  title string
  rules_text string?
  settlement_source string?
  resolves_at_ms int?
}

enum OutcomePolarity {
  DIRECT   @description("Contract B's YES means the same as Contract A's YES.")
  INVERTED @description("Contract B's YES means Contract A's NO.")
}

class EquivalenceVerdict {
  equivalent bool @description("True ONLY if the two contracts resolve identically in every possible outcome.")
  outcome_polarity OutcomePolarity
  reason string @description("One or two sentences: the decisive match, or the specific mismatch/ambiguity.")
}

function EvaluateEquivalence(contract_a: Contract, contract_b: Contract) -> EquivalenceVerdict {
  client Fireworks_DeepSeekV4Flash
  prompt #"
    You compare two binary event contracts from different exchanges to decide
    whether they resolve on the SAME acceptance criteria, so that holding
    offsetting legs forms a risk-free locked position. Be strict: a "locked"
    position built on a wrong equivalence verdict becomes unhedged risk.

    Check all four dimensions:
      1. Proposition: the precise event and threshold judged (e.g. "> 3.0%" vs
         ">= 3.0%" is a mismatch; note integer-reported quantities can make
         different-looking thresholds identical).
      2. Settlement source: which authority, dataset, index, exchange, or station
         determines the outcome.
      3. Timing/cutoff: observation date/window, time zone, whether revisions count.
      4. Edge cases: postponement, cancellation, ties, early resolution, void rules.

    `equivalent` is true ONLY if all four match (allowing wording differences
    that cannot change resolution). Any substantive difference, or ambiguity you
    cannot resolve from the text, means false.

    `outcome_polarity`: DIRECT if B's YES asserts the same outcome as A's YES;
    INVERTED if B's YES asserts A's NO (opposite sides of the same two-outcome
    event).

    {{ ctx.output_format }}

    Contract A:
      title: {{ contract_a.title }}
      native_id: {{ contract_a.native_id }}
      resolution_rules: {{ contract_a.rules_text }}
      settlement_source: {{ contract_a.settlement_source }}
      resolves_at_ms: {{ contract_a.resolves_at_ms }}

    Contract B:
      title: {{ contract_b.title }}
      native_id: {{ contract_b.native_id }}
      resolution_rules: {{ contract_b.rules_text }}
      settlement_source: {{ contract_b.settlement_source }}
      resolves_at_ms: {{ contract_b.resolves_at_ms }}
  "#
}

// ----------------------------------------------------------------------------
// Golden-set tests — runnable from the VSCode BAML UI (needs FIREWORKS_* env).
// ----------------------------------------------------------------------------

test sports_moneyline_direct_equivalent {
  functions [EvaluateEquivalence]
  args {
    contract_a {
      native_id "KXNBA-LAL"
      title "Will the Los Angeles Lakers win their game on 2026-06-14?"
      rules_text "Resolves YES if the Lakers win the scheduled NBA game vs Boston on 2026-06-14 per official NBA result. Void if postponed/cancelled."
      settlement_source "Official NBA result"
      resolves_at_ms 1781913600000
    }
    contract_b {
      native_id "lakers-beat-celtics"
      title "Lakers beat Celtics (June 14, 2026)"
      rules_text "YES if the Los Angeles Lakers defeat the Boston Celtics in their 2026-06-14 NBA game per official league result. Postponed games void."
      settlement_source "Official NBA result"
      resolves_at_ms 1781913600000
    }
  }
  @@assert(is_equivalent, {{ this.equivalent }})
  @@assert(polarity_direct, {{ this.outcome_polarity == "DIRECT" }})
}

test sports_opposite_side_inverted_equivalent {
  functions [EvaluateEquivalence]
  args {
    contract_a {
      native_id "KXNBA-LAL"
      title "Lakers win vs Celtics on 2026-06-14?"
      rules_text "YES if Los Angeles Lakers win the 2026-06-14 NBA game vs Boston. Two-team game, no draw. Void if not played."
      settlement_source "Official NBA result"
      resolves_at_ms 1781913600000
    }
    contract_b {
      native_id "celtics-win"
      title "Celtics win vs Lakers on 2026-06-14?"
      rules_text "YES if the Boston Celtics win the 2026-06-14 NBA game vs Los Angeles. Two-team game, no draw. Void if not played."
      settlement_source "Official NBA result"
      resolves_at_ms 1781913600000
    }
  }
  @@assert(is_equivalent, {{ this.equivalent }})
  @@assert(polarity_inverted, {{ this.outcome_polarity == "INVERTED" }})
}

test weather_bracket_equivalent_integer_reporting {
  functions [EvaluateEquivalence]
  args {
    contract_a {
      native_id "KXHIGHNY-26JUL05-T84"
      title "NYC high temp: 83° or below?"
      rules_text "If the highest temperature recorded in Central Park, New York for July 05, 2026 as reported by the National Weather Service's Climatological Report (Daily), is less than 84°, then the market resolves to Yes."
      settlement_source "NWS Climatological Report (Daily), Central Park"
      resolves_at_ms 1783400340000
    }
    contract_b {
      native_id "tc-temp-nychigh-2026-07-05-lt84f"
      title "Highest temperature in NYC on July 5?"
      rules_text "Will the highest temperature recorded at Central Park (KNYC) in New York City for 2026-07-05 as reported by the National Weather Service's Climatological Report (Daily) be less than or equal to 83F?"
      settlement_source "NWS Climatological Report (Daily), Central Park (KNYC)"
      resolves_at_ms 1783400400000
    }
  }
  @@assert(is_equivalent, {{ this.equivalent }})
  @@assert(polarity_direct, {{ this.outcome_polarity == "DIRECT" }})
}

test weather_threshold_mismatch_not_equivalent {
  functions [EvaluateEquivalence]
  args {
    contract_a {
      native_id "KXTEMP-NYC"
      title "NYC high temperature above 75°F on 2026-07-01?"
      rules_text "YES if the Central Park observed daily maximum temperature is strictly greater than 75°F on 2026-07-01."
      settlement_source "NWS Central Park station"
      resolves_at_ms 1782000000000
    }
    contract_b {
      native_id "nyc-temp-75"
      title "NYC high at least 75°F on 2026-07-01?"
      rules_text "YES if the Central Park daily maximum temperature is 75°F or higher on 2026-07-01."
      settlement_source "NWS Central Park station"
      resolves_at_ms 1782000000000
    }
  }
  // > 75 vs >= 75 differ exactly at 75, an outcome NWS integer reporting CAN produce.
  @@assert(not_equivalent, {{ not this.equivalent }})
}

test crypto_settlement_source_mismatch_not_equivalent {
  functions [EvaluateEquivalence]
  args {
    contract_a {
      native_id "KXBTC-CB"
      title "BTC at or above $100,000 by 2026-12-31?"
      rules_text "YES if BTC/USD trades at or above $100,000 on Coinbase before 2026-12-31 23:59 ET."
      settlement_source "Coinbase BTC-USD"
      resolves_at_ms 1798761540000
    }
    contract_b {
      native_id "btc-100k-binance"
      title "BTC reaches $100k by end of 2026?"
      rules_text "YES if BTC/USDT trades at or above $100,000 on Binance before 2026-12-31 23:59 UTC."
      settlement_source "Binance BTC-USDT"
      resolves_at_ms 1798761540000
    }
  }
  @@assert(not_equivalent, {{ not this.equivalent }})
}
