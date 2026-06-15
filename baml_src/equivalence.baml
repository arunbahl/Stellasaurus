// Resolution-equivalence acceptance-criteria evaluation (DESIGN §6.2).
// The model EXTRACTS each contract's acceptance criteria, then compares them
// dimension-by-dimension. `equivalent` is the boolean conjunction of all four
// dimension matches — there is no confidence score.

// Typed input: one contract's catalog fields. Rendering lives in the prompt
// (BAML), not in Python, so the LLM sees a consistent, structured description.
class Contract {
  native_id string
  title string
  rules_text string?
  settlement_source string?
  resolves_at_ms int?
}

class ContractCriteria {
  proposition string       @description("The precise event/threshold judged, e.g. 'CPI YoY > 3.0%'.")
  settlement_source string @description("The authority/dataset/index/exchange/station that determines the outcome.")
  timing_cutoff string     @description("Exact observation time, release date, or settlement window incl. time zone; whether revisions count.")
  edge_case_rules string   @description("Postponement, cancellation, ties, early resolution, scheduled-vs-actual, void/refund rules.")
}

class DimensionMatch {
  proposition bool
  settlement_source bool
  timing_cutoff bool
  edge_case_rules bool
}

enum OutcomePolarity {
  DIRECT   @description("Contract B's YES means the same as Contract A's YES.")
  INVERTED @description("Contract B's YES means Contract A's NO.")
}

class EquivalenceVerdict {
  contract_a_criteria ContractCriteria
  contract_b_criteria ContractCriteria
  dimension_match DimensionMatch
  equivalent bool          @description("True ONLY if every dimension_match field is true.")
  outcome_polarity OutcomePolarity
  rationale string
}

function EvaluateEquivalence(contract_a: Contract, contract_b: Contract) -> EquivalenceVerdict {
  client Fireworks
  prompt #"
    You compare two binary event contracts to decide whether they resolve on the
    SAME acceptance criteria, so that holding offsetting legs forms a risk-free
    locked position. Be strict: a "locked" position built on a wrong equivalence
    verdict becomes unhedged risk.

    For EACH contract independently, extract its acceptance criteria:
      - proposition / outcome judged (the precise event and threshold)
      - settlement source / reference (which authority, dataset, release, index,
        exchange, or station determines the outcome)
      - timing / cutoff / expiration (exact time incl. time zone; whether
        post-print revisions count)
      - edge-case and void rules (postponement, cancellation, ties, early
        resolution, scheduled-vs-actual, refund conditions)

    Then compare the two contracts dimension by dimension. Set each
    dimension_match field true ONLY if the two contracts match on that dimension.
    Trivial wording differences that do not change resolution are still a match;
    any substantive difference, or ambiguity you cannot resolve from the text, is
    NOT a match.

    Set `equivalent` true ONLY if all four dimension_match fields are true.

    Set `outcome_polarity`: DIRECT if Contract B's YES means the same as Contract
    A's YES; INVERTED if Contract B's YES corresponds to Contract A's NO (e.g. the
    two contracts back opposite sides of the same two-outcome event).

    {{ ctx.output_format }}

    {% for c in [contract_a, contract_b] %}
    Contract {{ "A" if loop.first else "B" }}:
      title: {{ c.title }}
      native_id: {{ c.native_id }}
      resolution_rules: {{ c.rules_text }}
      settlement_source: {{ c.settlement_source }}
      resolves_at_ms: {{ c.resolves_at_ms }}
    {% endfor %}
  "#
}

// ----------------------------------------------------------------------------
// Golden-set tests — run from the VSCode BAML UI (needs FIREWORKS_API_KEY_BAML).
// Cover sports (trivial), economics, weather, crypto with both EQUIVALENT and
// NOT_EQUIVALENT outcomes (DESIGN §11). `this` is the function's return value.
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

test economics_identical_equivalent {
  functions [EvaluateEquivalence]
  args {
    contract_a {
      native_id "KXCPI"
      title "US CPI YoY above 3.0% for May 2026?"
      rules_text "YES if the BLS CPI-U headline year-over-year inflation for May 2026, first print, is greater than 3.0%."
      settlement_source "US BLS CPI-U (NSA), first release"
      resolves_at_ms 1781913600000
    }
    contract_b {
      native_id "cpi-may-3pct"
      title "CPI year-over-year > 3.0% (May 2026)"
      rules_text "Resolves YES if the initial BLS CPI-U headline YoY figure for May 2026 exceeds 3.0%."
      settlement_source "US BLS CPI-U (NSA), first release"
      resolves_at_ms 1781913600000
    }
  }
  @@assert(is_equivalent, {{ this.equivalent }})
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
  // Strict > vs >= is a substantive proposition difference -> not equivalent.
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
  // Different reference exchange (and quote/timezone) -> not equivalent.
  @@assert(not_equivalent, {{ not this.equivalent }})
}
