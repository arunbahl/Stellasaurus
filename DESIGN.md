# Locked Cross-Venue Arbitrage System — Design Document

**Venues:** Kalshi (CFTC DCM) and Polymarket US (QCX, CFTC DCM)
**Status:** Design for implementation handoff
**Scope of this version:** Locked (risk-free) arbitrage only

---

## 1. Purpose and Scope

Build an automated system that detects and executes **locked arbitrage** between
binary event contracts listed on Kalshi and Polymarket US, where "locked" means
the two legs fully offset so the position pays a fixed amount at resolution
regardless of outcome, and the combined entry cost (including fees and slippage)
is strictly less than that payout.

### In scope
- Continuous detection of locked opportunities across both venues.
- Local, low-latency evaluation and execution.
- Resolution-equivalence verification of contract pairs, with explicit
  evaluation of each contract's **acceptance criteria**.
- Local fee computation, with asynchronous reconciliation against actual fees.
- A control dashboard with a fail-safe pause/kill switch.

### Explicit non-goals (this version)
- Statistical / convergence arbitrage or any position with outcome risk.
- Directional speculation or model-based "fair value" trading.
- Market making for rebates as a primary strategy (rebates are accounted for,
  not pursued).
- Venues other than Kalshi and Polymarket US.

---

## 2. Glossary

- **Contract / market:** A binary instrument paying $1.00 if its proposition
  resolves YES, $0.00 if NO. Price ∈ (0, 1) ≈ implied probability.
- **Acceptance criteria:** The exact, complete conditions under which a contract
  resolves YES — the proposition judged, the source of truth, the timing/cutoff,
  and the edge-case rules. The unit of equivalence comparison.
- **Canonical proposition:** For a verified pair, the single YES statement both
  contracts are mapped onto. Each venue's native YES may map to canonical YES or
  canonical NO (see `outcome_polarity`).
- **Locked pair:** Holding canonical-YES on one venue and canonical-NO on the
  other, in equal size, so exactly one leg pays $1 at resolution.
- **Net edge:** Payout ($1 per pair) minus all-in entry cost (both premiums +
  both fees + slippage).
- **θ (theta):** Minimum net edge per pair required to trade — the safety margin.
- **Hurdle rate (h):** Minimum annualized return on committed capital required to
  trade; rations scarce capital toward fast-resolving opportunities.
- **Hot path:** Code on the detect→decide→execute critical path. Local memory
  only; no network, LLM, or disk reads.
- **Background plane:** Slow, network/LLM/disk work that prepares state consumed
  by the hot path.

---

## 3. Trading Objective and Trade Acceptance Criteria (the math)

### 3.1 Objective
Capture price discrepancies between Kalshi and Polymarket US on
resolution-equivalent contracts, holding fully offsetting legs so the position
is outcome-neutral, executing only when all gates below pass.

### 3.2 The locked condition
For one canonical-YES contract and one canonical-NO contract held in equal size,
resolution pays exactly $1.00 per pair. With per-pair entry cost:

```
total_cost(pair) = cost_YES_leg + cost_NO_leg
                 = (price_YES + fee_YES) + (price_NO + fee_NO)

gross_edge = 1.00 - (price_YES + price_NO)
net_edge   = 1.00 - total_cost
           = gross_edge - fee_YES - fee_NO
```

There are two orientations to evaluate every cycle (the system must check both):

- **Orientation A:** buy canonical-YES on Kalshi, buy canonical-NO on Polymarket.
- **Orientation B:** buy canonical-YES on Polymarket, buy canonical-NO on Kalshi.

Prices are taken from the live local order book and walked for the target size
(see 3.4), and `fee_*` are computed locally (Section 6.4). "YES"/"NO" on each
venue are resolved through the pair's `outcome_polarity` mapping.

### 3.3 Trade gates (ALL must pass)
1. **Equivalence gate (hard precondition):** the pair exists in the Verified Pair
   Registry as `EQUIVALENT`. The hot path never re-derives this; it trusts the
   registry. (Section 6.2 / 6.3.)
2. **Net-edge gate:** `net_edge_per_pair ≥ θ` at the intended fill size.
3. **Capital-return gate:** annualized return on committed capital ≥ `h`:
   ```
   committed_capital(pair) ≈ price_YES + fee_YES + price_NO + fee_NO   # ≈ just under $1
   period_return           = net_edge / committed_capital
   T_days                  = days until both contracts resolve
   annualized_return       = period_return * (365 / T_days)            # simple; see config
   require: annualized_return ≥ h
   ```
4. **Depth gate:** sufficient resting depth on **both** legs to fill target size
   while net_edge stays ≥ θ after walking the books (slippage included in 3.2
   prices).
5. **Leg-fillability gate:** confidence both legs can be filled near-simultaneously
   under the chosen execution policy (Section 6.7).
6. **Risk-limit gate:** within per-event, per-venue, aggregate, and capital limits
   (Section 6.8), and the global halt flag is clear.

### 3.4 Size and slippage
For target size `Q`, compute the volume-weighted average price (VWAP) to fill `Q`
contracts on each leg by walking the local book. `price_YES` and `price_NO` in
3.2 are these VWAPs, so slippage is folded into net_edge directly. If `Q` cannot
be filled at depth keeping net_edge ≥ θ, reduce `Q` to the largest size that
satisfies the gates, or skip.

---

## 4. System Architecture Overview

Three planes. The governing rule: **the hot path reads only pre-loaded in-memory
state; everything expensive happens in the background plane and is pushed into
that state out of band.**

```
                          ┌─────────────────────────── CONTROL PLANE ───────────────────────────┐
                          │  Dashboard (read model)   •   Pause / Kill switch   •   Audit log     │
                          └───────▲──────────────────────────────────┬───────────────────────────┘
                                  │ status/metrics                    │ halt flag / commands
   ┌─────────────── BACKGROUND PLANE (slow; network, LLM, disk) ──────┴──────────────────────────┐
   │  Catalog Sync & Pairing  →  Resolution-Equivalence Engine  →  Verified Pair Registry (mem)   │
   │  Fee Parameter Sync  →  Fee Param Cache (mem)                                                 │
   │  Reconciliation & Calibration  ←──────────── fills / preview ────────────────────────────┐   │
   └───────────────────────────────────┬──────────────────────────────────────────────────────┘   │
                                        │ pushes: pair registry, fee params, limits (in memory)      │
   ┌──────────────── HOT PATH (fast; local memory only) ───────────────────────────────────────┐   │
   │  Market-Data Ingestion (dual stream) → Normalized Books (mem)                              │   │
   │        → Opportunity Evaluator → Risk Gate → Execution Engine → [single submission hop] ───┼───┘
   └─────────────────────────────────────────────────────────────────────────────────────────┘
```

Only the order submission in the Execution Engine performs a network round-trip
on the critical path. Market data arrives via push streams maintained in memory.

---

## 5. Technology Notes

- **Languages:** Polymarket US ships official `polymarket-us` SDKs for Python
  (3.10+) and TypeScript (Node 18+). Kalshi exposes REST/WebSocket/FIX with an
  OpenAPI/AsyncAPI spec for client generation. Recommend a single
  performance-capable language for the hot path (e.g., Rust or Go for the
  evaluator/ingestion/execution) and any convenient language (Python) for the
  background plane and dashboard. Cross-language is acceptable because the planes
  are decoupled through the in-memory state stores and a message bus.
- **Concurrency:** Hot path should be lock-free or use fine-grained locking;
  pre-allocate; avoid GC pauses in the evaluator if using a managed runtime.
- **State stores:** In-memory structures (registry, books, fee params, limits)
  for the hot path; a durable store (e.g., Postgres) for the pair registry source
  of truth, positions, audit log, and P&L.

---

## 6. Component Specifications

### 6.1 Catalog Sync & Pairing (background)
**Responsibility:** Maintain a current catalog of tradeable markets on both
venues and propose candidate pairs referencing the same underlying event.

- Poll/refresh catalogs on an interval (config: `catalog_refresh_seconds`).
  - Kalshi: enumerate series → events → markets.
  - Polymarket US: enumerate events → markets; use `subjects`
    (`GET /subjects`, `Get Markets For Subject`) and `series` for grouping.
- **Candidate generation strategies** (cheap, high-recall):
  - **Sports:** use structured sports data on both sides — league, teams,
    game date/time — to generate high-precision candidates. Polymarket exposes
    sports leagues/teams/schema; Kalshi exposes structured sports series.
    Polymarket's `Get Event By Partner External ID` can resolve a shared
    third-party fixture ID (e.g., OpticOdds) to a Polymarket event — use as a
    join key when available.
  - **Non-sports:** match on titles, subjects, tags, and resolution dates as a
    coarse filter to produce candidates for the equivalence engine.
- Emit candidate pairs to the Resolution-Equivalence Engine. Detect changes to
  contract terms and flag affected registry entries for re-evaluation.

### 6.2 Resolution-Equivalence Engine (background) — **acceptance-criteria evaluation**
**Responsibility:** Decide, for each candidate pair, whether the two contracts
resolve on the **same acceptance criteria** (and therefore can form a locked
position), and record the YES/NO polarity mapping.

Pipeline per candidate:

1. **Deterministic structured pre-check.** Where both venues provide structured
   identifiers (sports: teams/league/game datetime; events: subject/series), test
   them directly. A structured mismatch → `NOT_EQUIVALENT` immediately (no LLM
   call). A structured exact match on a fully-structured market type (e.g., a
   straightforward "team X wins game G" sports moneyline) → may be marked
   `EQUIVALENT` without LLM, with the matched fields recorded.

2. **LLM acceptance-criteria evaluation.** For everything not fully settled by
   step 1, call a fast lightweight model. Its explicit task is to **extract and
   compare the acceptance criteria of each contract**, not to summarize them.
   This is trivial for sports (who wins, in which game) but **not clear-cut** for
   economic, political, weather, settlement-price, and other contracts, where the
   resolution hinges on details that must be compared exactly. The model must
   evaluate, for each contract independently and then comparatively, these
   acceptance-criteria dimensions:

   - **Proposition / outcome judged:** the precise event and threshold (e.g.,
     "CPI YoY > 3.0%" vs "> 3%"; "candidate wins" vs "candidate inaugurated";
     ">75°F" vs "≥75°F").
   - **Source of truth / settlement reference:** which authority, dataset,
     release, index, exchange, or station determines the outcome (e.g., which CPI
     release and whether seasonally adjusted; which weather station; which crypto
     price index and venue; which official result).
   - **Timing / cutoff / expiration:** the exact observation time, release date,
     or settlement window, including time zone, and whether revisions after the
     first print count.
   - **Edge-case and void rules:** postponement, cancellation, ties, early
     resolution, "scheduled vs actual," and any void/refund conditions.

   **Output schema (no confidence scoring; binary verdict):**
   ```json
   {
     "contract_a_criteria": {
       "proposition": "...",
       "settlement_source": "...",
       "timing_cutoff": "...",
       "edge_case_rules": "..."
     },
     "contract_b_criteria": { "proposition": "...", "settlement_source": "...",
                              "timing_cutoff": "...", "edge_case_rules": "..." },
     "dimension_match": {
       "proposition": true,
       "settlement_source": true,
       "timing_cutoff": true,
       "edge_case_rules": true
     },
     "equivalent": true,
     "outcome_polarity": "DIRECT",        // or "INVERTED" (B's YES == A's NO)
     "rationale": "..."
   }
   ```
   `equivalent` is `true` only if **every** dimension in `dimension_match` is
   `true`. Any mismatch (including ambiguity the model cannot resolve from the
   contract text) yields `equivalent: false`. There is no confidence threshold;
   the verdict is the dimension-by-dimension boolean conjunction.

3. **Disposition.** `equivalent: true` → write/refresh a `VERIFIED` registry entry
   with the polarity mapping and the extracted criteria. `equivalent: false` →
   record as `NOT_EQUIVALENT` with rationale (so it is not re-evaluated until
   terms change).

**Placement guarantees:** This engine runs only in the background, at pairing
time and on terms-change, never on the trade hot path. The hot path consumes only
the resulting registry.

### 6.3 Verified Pair Registry (state; consumed by hot path)
Authoritative store of tradeable pairs. Source of truth in durable storage;
an immutable in-memory snapshot is published to the hot path on each update.

```
PairRegistryEntry {
  pair_id:            string
  canonical_proposition: string
  kalshi_ticker:      string
  poly_market_slug:   string
  outcome_polarity:   enum { DIRECT, INVERTED }   // maps each venue YES to canonical
  status:             enum { VERIFIED, NOT_EQUIVALENT, STALE }
  resolves_at:        timestamp                    // for T_days / hurdle
  acceptance_criteria: { ...extracted dimensions... }   // audit / display only
  last_verified_at:   timestamp
  terms_fingerprint:  string                       // detects contract-term changes
}
```
Only `status == VERIFIED` entries are streamed and evaluated. A terms change
flips the entry to `STALE` (removing it from the tradeable set) and re-queues it
for the equivalence engine.

### 6.4 Fee Engine (local compute + background sync + reconciliation)
**Hot-path responsibility:** compute exact fees locally; **never** request fees on
the critical path.

**Kalshi fee (event contracts):**
```
fee_per_order_kalshi(contracts C, price p, role) =
    accumulate_round( multiplier(series, role) * C * p * (1 - p) )
```
- `multiplier` from the cached fee params (Section: Fee Param Sync). Baseline
  taker ≈ 0.07; maker roughly 75% lower; fee *type* per series is one of
  `quadratic`, `quadratic_with_maker_fees`, `flat` (from `GET /series/fee_changes`).
- **Rounding:** replicate Kalshi's documented per-order fee accumulator and
  rounding. Target balance precision is $0.01 for standard accounts and $0.0001
  for direct members — make precision a config value. Getting this exact is
  required so reconciliation does not throw false divergence.

**Polymarket US fee:**
```
fee_poly(notional, role) =
    role == MAKER ? 0
                  : max(min_fee, taker_bps/10000 * notional)
notional = filled_contracts * avg_fill_price
```
- Defaults: `taker_bps = 10` (0.10%), `maker = 0`, `min_fee = $0.001`. Confirm
  live via the `preview` endpoint's `commissionsBasisPoints` in reconciliation.

**Fee Param Sync (background):** periodically refresh Kalshi multipliers/fee
types via `/series/fee_changes` and Polymarket bps via sampled `preview` calls;
publish to the in-memory Fee Param Cache. Config: `fee_param_refresh_seconds`.

**Reconciliation (background):** compare locally-computed fees with actual fees
returned by the venues — Kalshi `taker_fees_dollars` / `maker_fees_dollars` on
filled orders; Polymarket `commissionNotionalTotalCollected` from `preview` and
from fills. Divergence beyond `fee_divergence_tolerance` raises an alert and
trips the kill switch (Section 6.8/6.9).

**Rebates/incentives:** Polymarket liquidity/volume/maker incentive credits and
any Kalshi maker rebates are paid out of band. Do **not** net them into hot-path
fee math; account for them in the P&L/calibration layer only.

### 6.5 Market-Data Ingestion (hot path)
**Responsibility:** maintain live, normalized local order books for the markets
in the VERIFIED registry only.

- One streaming client per venue, lowest-latency transport available
  (Kalshi WebSocket/FIX; Polymarket gRPC/WebSocket/FIX). Subscribe only to
  registry markets (scoped subscription, not full-exchange).
- Normalize both feeds into a common internal book in **canonical-YES terms,
  USD**, applying `outcome_polarity` so a price comparison is apples-to-apples.
- Maintain BBO and depth ladder per market. Stamp each update with receipt time
  for latency metrics and staleness checks.
- **Staleness/disconnect:** if either venue's feed for a pair goes stale beyond
  `book_staleness_ms` or disconnects, mark the pair non-evaluable and signal the
  kill switch's auto-trigger (you cannot assert "locked" on a stale book).

```
NormalizedBook {
  venue:        enum { KALSHI, POLYMARKET }
  pair_id:      string
  yes_bids:     [ (price, size) ]   // canonical-YES terms
  yes_asks:     [ (price, size) ]
  no_bids:      [ (price, size) ]   // canonical-NO terms (derived/native)
  last_update:  monotonic_ts
}
```

### 6.6 Opportunity Evaluator (hot path) — the core loop
**Trigger:** any book update for a VERIFIED pair (event-driven, not polled).

```
on_book_update(pair_id):
    entry = registry_snapshot[pair_id]
    if entry.status != VERIFIED: return
    if not books_fresh(pair_id): return            # staleness gate

    for orientation in (A, B):                     # check both directions
        yes_venue, no_venue = orientation_venues(orientation)
        # target_size is capped by the live, UI-settable max bet value (in $):
        #   Q <= floor(max_bet_value / approx_cost_per_pair)
        # and may be further reduced by available book depth and other limits.
        Q = target_size(pair_id, risk_limits)      # honors max_bet_value + depth
        vwap_yes = walk_book_for_size(books[yes_venue].yes_asks, Q)
        vwap_no  = walk_book_for_size(books[no_venue].no_asks,  Q)
        if vwap_yes is None or vwap_no is None: continue   # depth gate fail

        fee_yes = fee_for(yes_venue, Q, vwap_yes, role_yes)
        fee_no  = fee_for(no_venue,  Q, vwap_no,  role_no)
        total_cost = vwap_yes + vwap_no + (fee_yes + fee_no)/Q   # per pair
        net_edge   = 1.00 - total_cost
        if net_edge < theta: continue                          # net-edge gate

        committed = vwap_yes + vwap_no + (fee_yes + fee_no)/Q
        T_days    = days_until(entry.resolves_at)
        ann_ret   = (net_edge / committed) * (365 / max(T_days, min_T_days))
        if ann_ret < hurdle: continue                          # capital gate

        intent = TradeIntent(pair_id, orientation, Q, vwap_yes, vwap_no,
                             role_yes, role_no, net_edge, ts=now())
        if risk_manager.approve(intent):                       # risk + halt gate
            execution_engine.submit(intent)
        return   # one intent per update; or rank and pick best orientation
```

The evaluator is pure local arithmetic over in-memory books and cached params.
Optimize aggressively; this is where latency advantage is won or lost.

### 6.7 Execution Engine (hot path) — leg-risk policy
**Responsibility:** place both legs and guarantee the position ends either fully
hedged or fully flat; never leave a hanging single leg.

- **Default policy — simultaneous fill-or-kill:** submit both legs with FOK (or
  IOC) time-in-force as close to simultaneously as possible. Both venues support
  FOK/IOC. If exactly one leg fills (rare), immediately force-unwind the filled
  leg as a marketable order. Budget for this tail loss in θ.
- **Optional maker policy (config per pair/opportunity):** rest a maker limit
  order on Kalshi (Polymarket `participateDontInitiate` / Kalshi resting limit)
  to capture the ~75%-lower maker fee, then take the other leg once the maker
  leg confirms. Lower fees, higher hang risk. The maker/taker choice is a
  per-trade decision driven by the fee-vs-leg-risk trade-off; default to taker
  both legs until live data justifies maker on a given pair.
- **Order construction:**
  - Polymarket intents: `BUY_LONG` = buy YES, `BUY_SHORT` = buy NO,
    `SELL_LONG`/`SELL_SHORT` for exits; map canonical side via polarity.
  - Kalshi: buy YES or buy NO directly via the order side/action fields.
- **Confirmation & unwind:** subscribe to private/order streams on both venues;
  reconcile fills; trigger the single-leg unwind path on partial/asymmetric fill;
  record realized fees for reconciliation (Section 6.4).
- **Batching:** both venues support batch order ops (≤ 20). Use where it reduces
  round-trips without adding latency to the binding leg.

### 6.8 Risk / Position / Capital Manager (hot path gate + state)
- **Limits:** max contracts per event, per venue exposure, aggregate open
  exposure, max simultaneous open pairs, max committed capital, and
  **maximum bet value** (see below). Limits are held in an in-memory snapshot
  the hot path reads; the durable store is the source of truth.
- **Maximum bet value (`max_bet_value`):** a hard dollar cap on the capital
  committed to any single arbitrage entry (one locked pair at the chosen size).
  It is enforced two ways:
  - **Sizing:** `target_size()` caps `Q` so that committed capital for the entry
    (`Q * (price_YES + fee_YES + price_NO + fee_NO)`) does not exceed
    `max_bet_value`; if even the minimum tradeable size would exceed the cap, the
    opportunity is skipped.
  - **Approval:** `approve()` rejects any `TradeIntent` whose committed capital
    exceeds the current `max_bet_value`, as a backstop independent of sizing.
  - **Runtime-settable from the UI** (Section 6.9), with a non-UI hard ceiling
    `max_bet_value_ceiling` that the UI value can never exceed.
- **Capital allocation:** committed capital is locked until resolution and cannot
  be withdrawn while positions are open; track free vs committed capital per
  venue. The hurdle rate (Section 3.3) is the primary rationing mechanism for
  scarce capital — prefer fast-resolving opportunities.
- **Halt flag:** single authoritative in-memory flag; `approve()` returns false
  whenever set. Settable by the dashboard and by automatic triggers (6.9).
- **Approve()** checks: halt flag clear, within all limits **including
  `max_bet_value`**, capital available for both legs, pair still VERIFIED and
  fresh.

### 6.9 Control Plane / Dashboard (control)
- **Read model:** live opportunities (and why each did/didn't fire), open
  positions and their hedge status, free/committed capital per venue, realized
  and expected edge, fee-reconciliation and latency metrics, feed health.
- **UI-settable controls (runtime, no redeploy):** operators can adjust selected
  limits live from the dashboard. These MUST include:
  - **Maximum bet value (`max_bet_value`)** — dollar cap on capital committed to
    any single arbitrage entry. This is the primary required control.
  - Also exposed: `max_aggregate_exposure`, `max_open_pairs`,
    `max_committed_capital`, `target_size_default`, `theta`, and `hurdle`.
  - **Propagation:** a UI change writes to the durable limits store, is validated
    (numeric, ≥ 0, and clamped to its non-UI hard ceiling — e.g.
    `max_bet_value` ≤ `max_bet_value_ceiling`), then published as a new immutable
    in-memory limits snapshot that the Risk Manager swaps in atomically. In-flight
    and already-open positions are unaffected; the new cap applies to the next
    `approve()`/sizing decision. All changes are written to the audit log with
    operator, timestamp, old value, and new value.
  - **Fail-safe:** an invalid or out-of-range value is rejected and the prior
    value retained; lowering `max_bet_value` (or any limit) to 0 is a valid way to
    stop new entries of that size without a full halt.
- **Pause / kill switch (fail-safe):**
  - **Manual:** pause (stop new entries) and global-flatten controls.
  - **Automatic triggers that set the halt flag:** loss/staleness of either
    venue's data feed; fee or slippage reconciliation divergence beyond
    tolerance; risk-limit breach; abnormal fill/reject rates; equivalence engine
    flagging an in-use pair STALE.
  - **Halt semantics (define explicitly):** halting stops new entries. It does
    **not** auto-liquidate already-hedged (safe) positions — those are allowed to
    resolve. It **does** immediately flatten any unhedged leg arising from a
    failed execution. Resting maker orders are cancelled on halt.
  - **Fail-safe default:** any uncertainty (disconnect, missing data, divergence)
    resolves to *not trading*.

### 6.10 Reconciliation & Calibration (background)
- Compare computed vs actual fees and computed vs realized slippage; feed drift
  alerts to the kill switch and corrections to the Fee Param Cache.
- Track realized net edge vs predicted; recommend θ and hurdle adjustments.
- Fold in out-of-band rebates/incentives for true P&L.

### 6.11 Observability & Audit (cross-cutting)
- Structured, append-only audit log of every decision with full inputs (book
  snapshot reference, computed fees, edges, gate results) and every order with
  outcome.
- Per-stage latency histograms (feed receipt → eval → submit → ack).
- Health/metrics endpoints for the dashboard and alerting.

---

## 7. External API Reference (integration facts)

### Kalshi
- **Base:** `https://api.kalshi.com/trade-api/v2`; demo environment available.
- **Transports:** REST, WebSocket, FIX. OpenAPI (`openapi.yaml`) and AsyncAPI
  (`asyncapi.yaml`) specs published for client generation.
- **Auth:** API key with signed requests (`KALSHI-ACCESS-KEY`,
  `KALSHI-ACCESS-SIGNATURE`, `KALSHI-ACCESS-TIMESTAMP` headers).
- **Resources:** `/markets`, `/events`, `/series`, `/orders`, `/portfolio`.
- **Fees:** `GET /series/fee_changes` returns fee type (`quadratic`,
  `quadratic_with_maker_fees`, `flat`). Order objects carry
  `taker_fees_dollars` / `maker_fees_dollars`. No pre-trade fee endpoint for
  event contracts → compute locally. (`/margin/fee_tiers` is perps-only — do not
  use for event contracts.)
- **Rate limits:** tiered token budgets; `GET /account/endpoint_costs` is the
  authoritative non-default cost list.

### Polymarket US
- **Base:** `https://api.polymarket.us`. Official SDKs: `polymarket-us`
  (Python 3.10+, TypeScript Node 18+). REST, gRPC, WebSocket, and FIX
  (institutional) available.
- **Auth:** Ed25519 API key. Headers `X-PM-Access-Key`, `X-PM-Timestamp`
  (ms, within 30s of server), `X-PM-Signature` (base64 Ed25519 of
  `timestamp + method + path`). Keys generated at `polymarket.us/developer`
  **after** completing identity verification in the iOS app. Public market-data
  endpoints need no auth; trading and `preview` require auth.
- **Market data:** `Get Markets`, `Get Market By Slug/ID`, `Get Market Book`,
  `Get Market BBO`, `Get Market Settlement`, order-book endpoints; WebSocket
  (`markets`, `private`) and gRPC streaming.
- **Pairing aids:** `events`, `subjects` (`Get Markets For Subject`), `series`,
  sports (`Get Sports Teams`, leagues, sports schema), and
  `Get Event By Partner External ID` (resolves third-party fixture IDs, e.g.
  OpticOdds).
- **Trading:** `Create Order`, `Preview Order` (returns `Order` with
  `commissionsBasisPoints`, `makerCommissionsBasisPoints`,
  `commissionNotionalTotalCollected`, `avgPx`), cancel/modify, batch ≤ 20,
  `Close Position Order`. Intents: `BUY_LONG`=YES, `SELL_LONG`=sell YES,
  `BUY_SHORT`=NO, `SELL_SHORT`=sell NO. TIF: DAY, GTC, GTD, IOC, FOK.
  `participateDontInitiate` = maker-only. `slippageTolerance` in bips or ticks.
- **Fees:** flat 0.10% taker, 0% maker, $0.001 minimum → compute locally;
  verify via `preview` in reconciliation.

---

## 8. Latency Design

- **Budget components:** (a) venue → us market-data propagation; (b) local
  evaluation; (c) us → venue order submission. You fully control (b); minimize
  with tight data structures, no allocation in the loop, no GC pauses.
- **Transport:** prefer the lowest-latency offered by each venue (FIX/gRPC over
  REST polling) for both data and order entry.
- **Hosting/region:** co-locate near each venue's gateways; Polymarket US infra
  is US-optimized (NY region recommended in their docs). Evaluate placement that
  minimizes the max of the two venues' round-trips, since both legs must fire.
- **No network/LLM/disk in the hot path.** Registry, fee params, and limits are
  pre-loaded in memory and refreshed out of band.

---

## 9. Configuration Parameters

`[UI]` = adjustable live from the dashboard (bounded by any paired ceiling);
others are static/startup config.

```
theta                       # [UI] min net edge per pair ($)
hurdle                      # [UI] min annualized return on committed capital
min_T_days                  # floor on T_days to bound annualization
target_size_default         # [UI] default Q (contracts)
max_bet_value               # [UI] hard $ cap on capital committed per single entry
max_bet_value_ceiling       # non-UI absolute ceiling; UI max_bet_value <= this
max_contracts_per_event
max_exposure_per_venue
max_aggregate_exposure      # [UI]
max_open_pairs              # [UI]
max_committed_capital       # [UI]
book_staleness_ms
fee_divergence_tolerance
slippage_tolerance_bips
catalog_refresh_seconds
fee_param_refresh_seconds
kalshi_fee_multiplier_default        # 0.07 baseline
kalshi_balance_precision             # 0.01 standard / 0.0001 direct member
poly_taker_bps_default               # 10
poly_min_fee                         # 0.001
execution_policy_default             # TAKER_BOTH | MAKER_KALSHI
llm_model                            # fast lightweight model id
```

Runtime `[UI]` changes follow the safe-propagation path in Section 6.9 (validate
→ clamp to ceiling → atomic in-memory snapshot swap → audit-logged) and apply to
the next decision only; open positions are unaffected.

---

## 10. Failure Modes and Fail-Safe Behavior

| Condition | Detection | Response |
|---|---|---|
| Data feed disconnect/stale | `book_staleness_ms`, heartbeat | Pair non-evaluable; auto-halt new entries |
| Fee/slippage divergence | Reconciliation vs actuals | Alert; auto-halt; refresh fee params |
| Single-leg fill | Order/private stream | Immediate forced unwind of filled leg |
| Risk-limit breach | Risk manager | Block intent; alert |
| Pair terms changed | Catalog fingerprint diff | Mark STALE; remove from tradeable set; re-queue equivalence |
| Equivalence reversal | Re-evaluation says NOT_EQUIVALENT | Remove from tradeable set; flag open positions for review |
| Venue outage / auth failure | API errors | Auto-halt; surface on dashboard |

Default for any unhandled uncertainty: **do not trade.**

---

## 11. Testing and Validation Plan

- **Unit:** fee functions for both venues, including Kalshi rounding/accumulator
  exactness against published examples; book-walking VWAP; net-edge and
  annualization math; polarity mapping.
- **Equivalence:** golden set of contract pairs across sports (trivial),
  economics, politics, weather, and crypto (subtle acceptance criteria) with
  expected EQUIVALENT/NOT_EQUIVALENT verdicts and correct polarity; verify the
  engine never marks ambiguous pairs equivalent.
- **Replay/sim:** feed recorded dual-venue order-book data through the evaluator;
  assert it only fires when gates pass; measure would-be P&L.
- **Demo/paper:** run against Kalshi demo and any Polymarket sandbox; exercise
  partial-fill and single-leg-unwind paths.
- **Latency:** per-stage benchmarks; regression thresholds on the eval stage.
- **Kill switch:** verify every automatic trigger sets the halt flag and that
  halt semantics behave as specified (safe positions kept, unhedged legs flattened,
  maker orders cancelled).
- **Reconciliation:** inject fee-schedule changes; confirm divergence detection
  and auto-halt.

---

## 12. Assumptions, Prerequisites, and Open Items

- **Access prerequisites:** an identity-verified Polymarket US account with
  generated API keys (iOS-app KYC), and a Kalshi account with API keys. Confirm
  the trading entity's **state eligibility** on both venues before going live.
- **Equivalence residual risk:** per direction, the LLM verdict is treated as
  authoritative (no confidence gate). A wrong equivalence verdict converts a
  "locked" trade into unhedged risk; the conservative binary conjunction (any
  dimension mismatch → not equivalent) and STALE-on-terms-change are the only
  safeguards in this version. Consider keeping an audit review of the registry as
  an operational practice.
- **Capital is the binding constraint**, not opportunity count: positions lock
  capital until resolution and withdrawals are slow; size the capital pool and
  hurdle accordingly.
- **Open items to confirm during build:** exact Kalshi predictions fee
  multipliers per category via `fee_changes`; whether Polymarket `preview.avgPx`
  reflects full book depth (would let it serve as an out-of-band slippage check);
  Polymarket WebSocket/stream per-connection instrument caps; current rate-limit
  numbers on both venues; whether any Polymarket markets carry category-specific
  fee exceptions.

---

## 13. Suggested Build Phasing

1. **Read-only spine:** dual market-data ingestion + normalized books + catalog
   sync + Verified Pair Registry (manual seed) + dashboard read model. No trading.
2. **Equivalence engine:** structured pre-check + LLM acceptance-criteria
   evaluation populating the registry; golden-set tests.
3. **Evaluator + fees:** local fee engine, net-edge/annualization gates, both
   orientations; surface (paper) opportunities on the dashboard.
4. **Execution + risk:** FOK-both-legs execution, single-leg unwind, risk/capital
   manager, kill switch with auto-triggers; demo/paper trading.
5. **Reconciliation + calibration:** fee/slippage reconciliation, drift-driven
   auto-halt, θ/hurdle calibration from realized data.
6. **Latency hardening + go-live:** transport/region optimization, hot-path
   profiling, then small-size live trading behind tight limits.
