# Stellasaurus — Build Plan (Phase 1 deep, Phases 2–6 sketched)

## Context

`DESIGN.md` specifies a **locked cross-venue arbitrage system** between Kalshi and
Polymarket US: detect resolution-equivalent binary contracts, hold fully
offsetting legs so the position pays a fixed $1/pair regardless of outcome, and
trade only when net edge (after fees/slippage) clears safety + capital-return
gates. The repo is greenfield — only `DESIGN.md` exists.

The doc's architecture is three decoupled planes: a latency-sensitive **hot path**
(ingestion → normalized books → evaluator → risk gate → executor) that reads only
pre-loaded in-memory state, a **background plane** (catalog sync, equivalence
engine, fee sync, reconciliation), and a **control plane** (dashboard + kill
switch). We build it bottom-up over the doc's six phases; this plan implements
**Phase 1 (the read-only spine)** in depth and lays explicit seams for 2–6.

### Decisions locked with the user
- **Language:** all-Python, **target Python 3.12** (satisfies `kalshi-sdk` ≥3.12
  and `polymarket-us` ≥3.10). Hot path isolated behind a clean boundary so it can
  be re-implemented in Go later only if benchmarks demand it.
- **Target:** **paper/sim first** — live read-only market data + simulated
  executor, no real money. Real order path is built but **hard-gated off**
  (`live_trading_enabled=False`). Runs **keyless on public market data**; auth
  signing is written and unit-tested but only invoked when credentials exist.
- **Durable store:** local **SQLite** file (WAL), in front of which sit immutable
  in-memory snapshots for the hot path. Only background/control touch SQLite.
- **Equivalence LLM (Phase 2, sketched):** **Fireworks AI** model behind **BAML**
  (BoundaryML) for structured acceptance-criteria extraction.

### Grounded external facts (from research)
- **Polymarket US:** `pip install polymarket-us` (import `polymarket_us`). Ed25519
  auth (`X-PM-Access-Key`, `X-PM-Timestamp` ms, `X-PM-Signature` over
  `{ts}|{METHOD}|{path}`). Public REST: `/v1/markets`, `/v1/market/slug/{slug}`,
  `/v1/markets/{slug}/book`, `/v1/markets/{slug}/bbo`, `/v1/events*`. WS
  `wss://api.polymarket.us/v1/ws/markets` (subscribe by `marketSlugs`,
  **100 markets/connection cap**, `px`/`qty`). Sandbox is email-gated
  (onboarding@polymarket.us) → treat as not-immediately-available.
- **Kalshi:** pin `kalshi-sdk` **or** generate from OpenAPI; behind a thin adapter
  either works. RSA-PSS SHA256 auth, sign `{ts}{method}{path}` (strip query). WS
  `wss://external-api-ws.kalshi.com/trade-api/ws/v2` (demo `*.demo.kalshi.co`);
  channels `orderbook_delta`+`ticker` (snapshot→delta), subscribe by
  `market_ticker(s)`, **5 connections/user**. Quadratic fee `0.07·P·(1−P)` with
  published worked examples to unit-test against. Demo at `demo-api.kalshi.co`.

---

## Phase 1 — Read-only spine (build in depth)

Dual market-data ingestion + normalized canonical-YES books + catalog sync +
**manually-seeded** Verified Pair Registry + dashboard read model. **No evaluator
firing, no execution** — but every seam those need is left explicit.

### Package layout (`src/stellasaurus/`)
```
common/      types, money (micro-USD ints, NO float), ids (pair_id, terms_fingerprint),
             clock, config (pydantic-settings), logging (structlog + audit helper)
hot_path/    *** GO-REWRITABLE BOUNDARY — no asyncio/SDK/sqlite/pydantic imports ***
             book.py (NormalizedBook, walk_book_for_size), normalize.py (polarity + NO-derivation),
             snapshot.py (RegistrySnapshot, LimitsSnapshot, AtomicRef), state.py (HotState protocol),
             staleness.py, ingest.py (BookStore writer), seams.py (Evaluator/RiskGate/Executor/FeeEngine protocols)
venues/      base.py (VenueClient/VenueStream protocols + Raw* DTOs), sharding.py,
             kalshi/{client,stream,parse}.py, polymarket/{client,stream,parse}.py
background/  catalog_sync.py, registry_loader.py, subscription_mgr.py, scheduler.py
storage/     db.py, schema.sql, markets_repo.py, registry_repo.py, audit_repo.py
control/     app.py (FastAPI), readmodel.py, routes.py, ws.py, static/
app.py       composition root: config → db → state → venues → background → control
seeds/pairs.seed.yaml   manual VERIFIED pair seed (Phase-1 stand-in for the LLM)
```

### Hot-path boundary (the one seam that survives a Go rewrite)
`hot_path/state.py` exposes the **only** thing future hot-path code may read:
```python
class HotState(Protocol):
    def registry(self) -> RegistrySnapshot: ...
    def limits(self) -> LimitsSnapshot: ...
    def book(self, pair_id, venue) -> NormalizedBook | None: ...
    def is_fresh(self, pair_id) -> bool: ...        # both legs within book_staleness_ms
    def feed_health(self) -> FeedHealth: ...
```
Ingestion writes via a separate `BookStore` API; dashboard (now) and evaluator
(Phase 3) only read `HotState`. The `hot_path/` package imports no asyncio, SDK,
SQLite, or pydantic — pure dataclasses + pure functions.

### Core data structures
- **`NormalizedBook`** (frozen, slots): `venue, pair_id, yes_bids/asks,
  no_bids/asks` (canonical-YES terms, prices as **integer micro-USD**), `seq`,
  `recv_mono_ns`, `recv_wall_ms`, `no_side_source ∈ {NATIVE,DERIVED}`. Each update
  builds a new frozen book and atomically replaces the per-(pair,venue) ref.
- **`normalize(raw, entry) -> NormalizedBook`** — pure function, the
  highest-value test surface. `DIRECT`: native YES = canonical YES. `INVERTED`:
  native YES = canonical NO (swap ladders). When a venue streams only one side,
  derive the complement by reflection (`1 − p`) and tag `no_side_source=DERIVED`
  so the Phase-3 evaluator never walks synthetic depth as real.
- **`PairRegistryEntry`** (per DESIGN §6.3): `pair_id, canonical_proposition,
  kalshi_ticker, poly_market_slug, outcome_polarity, status, resolves_at,
  acceptance_criteria, last_verified_at, terms_fingerprint, source`.
- **Snapshots + `AtomicRef[T]`**: writer builds a new immutable snapshot off to
  the side, then a single `publish()` rebinds one attribute. Under CPython's GIL a
  single attr load/store is atomic → readers are lock-free and never see a torn
  object. (Go equivalent: `atomic.Pointer[T]`.) `RegistrySnapshot` carries a
  pre-filtered `verified` tuple for the hot path.

### Concurrency model — **single process, single asyncio loop** (no threads/procs)
Phase-1 work is I/O-bound (2 WS clients, periodic REST polls, FastAPI); the
CPU-heavy evaluator that would justify a second core doesn't exist yet. One writer
per resource + immutable snapshots makes correctness trivial. asyncio lives only
in `venues/*/stream.py`, `background/scheduler.py`, `control/` — never in
`hot_path/`. Tasks: N sharded WS readers/venue, catalog-sync, registry-refresh,
staleness sweeper, FastAPI server + WS push; `scheduler.py` supervises with
restart-on-crash + backoff. Evaluator (Phase 3) attaches event-driven to a
book-publish callback already emitted by `BookStore`.

### Venue adapters
`venues/base.py` protocols (`VenueClient.list_markets/get_book`,
`VenueStream.subscribe/updates`) keep catalog + normalization venue-agnostic; DTOs
are native-terms, conversion to canonical micro-USD happens in
`parse.py`→`normalize.py`. **Sharding** (`sharding.py`): Polymarket ≤100
slugs/WS-connection; Kalshi ≤5 connections packing tickers via `market_tickers`.
**Keyless degradation:** if a venue's WS requires an authenticated session and no
creds are present, the stream **falls back to REST book polling** producing
identical `RawBook` → identical downstream path. Ed25519 / RSA-PSS signers are
implemented + unit-tested now, invoked only when `credentials_present`.

### Catalog sync + manual seed
`catalog_sync.py` (every `catalog_refresh_seconds`): Kalshi series→events→markets,
Polymarket events→markets (public); compute `terms_fingerprint` over canonicalized
(title, rules, resolves_at, settlement fields); upsert `markets`; a changed
fingerprint on a registry-referenced market flips the pair to `STALE` + audit row.
`registry_loader.py`: parse `seeds/pairs.seed.yaml` (human-asserted VERIFIED
pairs) → join `markets` for `resolves_at`/fingerprint → upsert `pair_registry` →
build + `publish()` a fresh `RegistrySnapshot`.

### SQLite schema (`storage/schema.sql`, WAL + foreign_keys)
Tables: `markets(venue, native_id PK, title, rules_text, settlement_source,
resolves_at, status, terms_fingerprint, raw_json, …)`, `pair_registry(pair_id PK,
…, outcome_polarity, status, acceptance_criteria JSON, terms_fingerprint, source)`,
`audit_log(id, ts_ms, actor, event_type, pair_id, detail_json)` append-only, plus
**stubs `positions` and `pnl`** created now (so later migrations don't churn the
spine) and `schema_migrations`.

### Dashboard read model (FastAPI + uvicorn + WS push, same loop)
Shows: per-connection feed health (connected, last-frame age, frames/sec,
reconnects, per-pair latency, fresh/stale); normalized canonical-YES BBO per
VERIFIED pair side-by-side with a **display-only** `gross_edge` for both
orientations (not an evaluator firing); registry contents; catalog stats.
`readmodel.py` only calls `AtomicRef.get()` (lock-free, immutable) → can never
stall the hot path. Routes: `/health`, `/pairs`, `/catalog/stats`,
`/books/{pair_id}`; WS broadcasts a snapshot every ~250 ms.

### Config (`common/config.py`, pydantic-settings, `STELLA_` env + TOML)
Phase-1: `db_path`, `seed_path`, `catalog_refresh_seconds`, `book_staleness_ms`,
venue REST/WS URLs, `poly_markets_per_conn=100`, `kalshi_max_ws_conns=5`,
dashboard host/port, optional creds (absent → keyless mode). Carries §9 `[UI]`
params (`theta`, `hurdle`, `target_size_default`, `max_bet_value` +
`max_bet_value_ceiling`, exposure/open-pairs/committed-capital caps) as read-only
defaults now; control-plane editing lands in Phase 4. Fee params
(`kalshi_fee_multiplier_default=0.07`, `poly_taker_bps_default=10`, …) loaded into
a cache stub for Phase 3. **Money is micro-USD integers everywhere** (Poly
`$0.001` min fee and Kalshi `$0.0001` direct-member precision make cents too
coarse).

### Critical files
- `src/stellasaurus/hot_path/normalize.py` — canonical-YES + polarity + NO-derivation (most-tested)
- `src/stellasaurus/hot_path/snapshot.py` — `AtomicRef` immutable publish/swap (boundary core)
- `src/stellasaurus/venues/base.py` — venue-agnostic interface
- `src/stellasaurus/storage/schema.sql` — durable source of truth
- `src/stellasaurus/app.py` — composition root

---

## Phases 2–6 (sketched; seams reserved now)

2. **Equivalence engine (BAML + Fireworks):** `background/equivalence/` with a
   structured pre-check (sports/structured fields) + BAML function over a Fireworks
   model producing the DESIGN §6.2 schema (`dimension_match`, `outcome_polarity`,
   `equivalent`) → writes the same `pair_registry` rows with `source='LLM'`.
   Candidate generation (subjects/series, sports partner-external-id join) feeds it.
   `catalog_sync`'s terms-change→STALE already provides the re-evaluation trigger.
   Golden-set tests across sports/econ/politics/weather/crypto.
3. **Evaluator + fee engine:** implement `Evaluator.on_book_update` (event-driven
   off the existing book-publish callback) + `FeeEngine` (Kalshi quadratic
   accumulator w/ exact rounding, Poly bps), both orientations, net-edge +
   annualization gates; surface **paper** opportunities on the dashboard. Fee math
   unit-tested against published examples.
4. **Execution + risk + kill switch:** `RiskGate.approve` + `Executor.submit`
   (FOK-both-legs, single-leg forced unwind), risk/capital manager reading
   `LimitsSnapshot`, control-plane UI edits → `publish()` + audit, halt flag
   (`AtomicRef[bool]`) with auto-triggers (staleness/divergence/limit breach). Real
   order path in `venues/*/client.py` stays behind `live_trading_enabled` (off);
   demo/paper trading first.
5. **Reconciliation + calibration:** `background/reconciliation/` consumes
   `positions`/`pnl` + private fill streams (same adapter interface), compares
   computed vs actual fees/slippage, drift → auto-halt, recommends θ/hurdle.
6. **Latency hardening / optional Go rewrite:** profile the hot path; because
   `hot_path/` is asyncio/SDK/sqlite-free and consumes only `HotState` + immutable
   snapshots, it can be re-implemented in Go behind the same boundary if needed.

---

## Verification (Phase 1)

- **Unit:** `normalize` (DIRECT/INVERTED, native vs derived NO, price-unit
  conversion, ladder ordering, empty/one-sided); `walk_book_for_size` VWAP +
  insufficient-depth→None; `terms_fingerprint` stability/sensitivity; `money` no-
  float invariants; signer byte-exactness vs fixed vectors; fee-math stubs vs
  Kalshi published examples.
- **Replay:** record real public WS frames to `tests/replay/fixtures/*.jsonl`; a
  `ReplaySource` implementing `VenueStream` drives `parse→normalize→BookStore`;
  assert resulting book sequence (deterministic, no network).
- **Live smoke (keyless, `@pytest.mark.live`):** seed 1–2 real current pairs, run
  ~60 s against public Polymarket + Kalshi reads; assert catalog populated,
  registry has VERIFIED pairs, both feeds yield ≥1 normalized book, freshness flips
  to fresh, `/health` returns connected.
- **Run it:** `pip install -e ".[bg,control,dev]"` then
  `python -m stellasaurus.app` → open `http://127.0.0.1:8080` → confirm live
  canonical-YES BBO per pair from both venues with freshness = fresh.

## Risks to confirm during build
1. **Polymarket WS may require Ed25519 auth even for market data** → keyless
   Phase 1 falls back to REST book polling for Poly. Verify early (biggest unknown).
2. **Kalshi WS needs an authenticated session** → same REST-poll fallback; confirm
   public REST book endpoints + rate limits tolerate registry size at poll cadence.
3. **NO-side derivation** (`1−p` reflection) has no independent resting liquidity —
   fine for display, must be tagged `DERIVED` so the evaluator never treats it as real.
4. **SDK lag:** keep `venues/*/client.py` thin enough to swap SDK ↔ raw `httpx`.
5. **terms_fingerprint tuning:** too sensitive → STALE churn; too loose → misses a
   real rules change (the core safety property). Explicit field set + audit logging.
6. **Sandbox/KYC dependency:** real execution testing (Phase 4) blocks on Poly KYC
   + venue API keys — a scheduling dependency to surface now.
