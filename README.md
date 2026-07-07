# Stellasaurus

Locked cross-venue arbitrage for binary event contracts on **Kalshi** and
**Polymarket US**. It detects resolution-equivalent contract pairs across the two
venues and, when the fee-adjusted edge clears the safety and capital-return gates,
holds fully offsetting legs so the position pays a fixed **$1/pair** regardless of
outcome. See [`DESIGN.md`](./DESIGN.md) for the full design and [`PLAN.md`](./PLAN.md)
for the build plan.

> **Status: all six phases built; paper-first.** The full pipeline runs
> autonomously — discover → verify equivalence → stream books → evaluate →
> execute → settle. Live order execution has been validated on both production
> venues (including the failure paths), but **real trading stays hard-gated off
> by default** (`live_trading_enabled=false`). In the default mode the system
> trades on paper against live market data.

## What it does

1. **Catalog sweep** — rotates through every Kalshi series and the Polymarket US
   catalog (no predefined categories; structural exclusions only), plus a fast
   near-resolution priority sweep so game-day markets are paired before they start.
2. **Equivalence** — deterministic structured matchers first (weather brackets,
   dated ranges — zero LLM), then an LLM verdict (Fireworks behind BAML) for the
   rest. Verified pairs land in the Pair Registry with a canonical-YES polarity.
3. **Streaming** — both venues over WebSocket, normalized to canonical-YES books
   in integer micro-USD; feed-level staleness gating.
4. **Evaluation** — per book update, both orientations, venue-verified quadratic
   fee models (Kalshi `0.07·C·p·(1−p)`; Polymarket `0.06·C·p·(1−p)`), a VWAP walk,
   then the θ (net-edge) and hurdle (annualized-return) gates.
5. **Execution** — FOK on both legs concurrently. Both fill → HEDGED; one fills →
   forced unwind; unwind fails → **HANGING → halt → auto-flatten**. Paper and live
   share the same code path and position store.

## Architecture (three planes)

- **Hot path** (`stellasaurus.hot_path`) — pure, dependency-free: normalized books,
  immutable snapshots (`AtomicRef`), evaluator, fee math, risk gate, paper executor.
  No asyncio/SDK/SQLite/pydantic imports — the boundary that could be re-implemented
  in Go later. Reads only pre-loaded in-memory state.
- **Background plane** (`stellasaurus.background`, `.venues`, `.storage`) — catalog
  sync, equivalence/pairing loop, WebSocket feeds, fee-drift sync, live execution
  engine + auto-flattener, SQLite persistence.
- **Control plane** (`stellasaurus.control`) — FastAPI dashboard + read model +
  kill switch, reachable from localhost + tailnet only (never the LAN).

## Safety architecture

Money-path invariants, most learned the hard way and pinned by tests:

- **Hard gate** — no real order is ever sent unless `live_trading_enabled=true`;
  the gateways self-refuse per submit *and* the composition root won't wire the
  live engine without it. Both credentials must also be present.
- **Never a hanging leg** — a single-leg fill is force-unwound; if that fails the
  system halts and a background flattener owns the naked leg, closing it with
  escalating marketable orders and re-verifying against the venue until flat.
  Flattening the risk is automatic; **resume is manual by design.**
- **In-flight reservations** — the risk gate reserves a slot synchronously at
  approval so async execution can't flood past `max_open_pairs` /
  `max_committed_capital` before positions record. Reservations carry a TTL
  (orphan safety net) and a HANGING hold (a naked leg's slot never expires).
- **Re-entry cooldown** — a non-hedged outcome quarantines the pair briefly so a
  chronically half-filling pair can't churn losses tick-by-tick.
- **Kill switch + auto-halt** — manual halt plus automatic triggers (all-stale
  feeds, fee divergence, a hanging leg). Halt blocks all new entries.
- **Fill truth** — fills are read from authoritative order lookups / venue
  positions (Polymarket's create response omits fills and lags), never assumed.

## Quick start

```bash
uv venv --python 3.12
source .venv/bin/activate
uv pip install -e ".[bg,control,dev]"

python -m stellasaurus.app
# dashboard: http://127.0.0.1:8770
```

Runs against live market data once credentials are set (see below). Kalshi REST is
public/keyless; Polymarket US requires credentials for non-stale data, and both
require credentials for WebSocket streaming. Without any credentials the spine
still starts (catalog + registry + dashboard) on whatever public data is available.

## Credentials

Copy `.env.example` to `.env` (gitignored) and fill in what you have:

| Variable | Purpose |
| --- | --- |
| `STELLA_KALSHI_API_KEY_ID`, `STELLA_KALSHI_PRIVATE_KEY_PATH` | Kalshi WS streaming + live orders (RSA PEM file) |
| `STELLA_POLY_ACCESS_KEY`, `STELLA_POLY_ED25519_SEED` | Polymarket US data + orders (base64 32-byte Ed25519 seed) |
| `FIREWORKS_LLM_ENDPOINT`, `FIREWORKS_API_KEY_BAML` | Equivalence LLM (BAML client; `_BAML` is the full `Authorization` header) |

Set `STELLA_KALSHI_ENV=demo` to point Kalshi at its fake-money demo environment
(separate demo keys) for order-shape validation.

## Going live

Real trading is off until you explicitly opt in, and only after paper data shows
edges that survive the fresh-quote re-check:

```bash
STELLA_LIVE_TRADING_ENABLED=true \
STELLA_MAX_OPEN_PAIRS=1 STELLA_TARGET_SIZE_DEFAULT=1 \
STELLA_MAX_BET_VALUE_MICROS=2000000 STELLA_MAX_COMMITTED_CAPITAL_MICROS=1500000 \
python -m stellasaurus.app
```

Keep `theta_micros` positive so it fires only genuine edges. The kill switch,
reservation caps, cooldown, and auto-flattener are all active in live mode.

## Operating & recovery

The app is one long-lived process. It logs structured events to stdout and keeps
durable state in SQLite (`STELLA_DB_PATH`, default `data/stella.db`); the hot
path is in-memory and rebuilt on start.

```bash
python -m stellasaurus.app          # foreground; Ctrl-C = graceful shutdown
# background: run under your process manager and capture stdout to a log file.
pkill -f stellasaurus.app           # stop a backgrounded instance
```

**Observe** (dashboard at `:8770`, or curl the JSON):

| Endpoint | Shows |
| --- | --- |
| `GET /health` | per-feed connected/frames/last-frame, latency p50/p95, per-pair freshness |
| `GET /positions` | totals (`halted`, `open_pairs`, `committed`, realized P&L) + open positions + recent risk decisions |
| `GET /opportunities` | latest per-pair evaluations (net edge, the gate that blocked, would-fire) and paper fires |
| `GET /pairs`, `/books`, `/catalog/stats` | verified registry, live canonical-YES books, catalog counts |

**Control** (also the dashboard buttons):

```bash
curl -XPOST :8770/halt   -d '{"halted":true,"reason":"manual"}'   # kill switch
curl -XPOST :8770/halt   -d '{"halted":false}'                    # resume
curl -XPOST :8770/limits -d '{"theta_micros":30000,"max_open_pairs":2}'  # live-edit caps
```

**Failure modes** — what the system does, and what you do:

| Symptom / log event | Automatic response | Operator action |
| --- | --- | --- |
| A feed disconnects | supervisor reconnects with backoff; books go stale → non-evaluable | none unless persistent; check `/health` |
| All feeds stale past threshold | **auto-halt** | investigate connectivity, then resume |
| Fee params diverge from venue | **auto-halt** (`fee_sync`) | verify fee model vs venue, resume |
| One leg fills, other doesn't | forced unwind → `UNWOUND` (small loss); pair enters cooldown | none |
| Unwind fails (`…HANGING_leg_HALTING`) | **halt** + auto-flattener owns the naked leg, retrying to flat (`flatten_success`) | none if it flattens; if `flatten_EXHAUSTED_still_naked`, flatten manually (below) then review before resuming |
| Over-commit / duplicate fire | blocked by in-flight reservations + `max_open_pairs`/`max_committed_capital` | none |

**Reconcile & manual flatten.** Both venues' own position endpoints are the source
of truth — the app never assumes fills. If a naked leg persists (flattener budget
exhausted, or the app was down), read the live positions
(`GET /portfolio/positions` on Kalshi, `GET /v1/portfolio/positions` on Polymarket
with the same signed auth the app uses) and close any nonzero net with a marketable
reduce-only / opposite-side order. Do this **before** clearing halt. A restart
rebuilds the in-memory state and reservations from an empty store, so always
confirm both venues are flat (or intended-hedged) after any incident.

## Configuration

Settings load from `config/default.toml`, overridable via `STELLA_*` environment
variables — precedence: process env > `.env` > TOML > field defaults. All money
values are integer **micro-USD** ($1.00 = 1,000,000). See
`stellasaurus.common.config.Settings` for the full list; the `[UI]`-tagged risk
limits (θ, hurdle, sizing, exposure caps) become editable from the dashboard.

## Tests & checks

```bash
pytest -m "not live"          # fast, offline: unit + replay (pass this explicitly)
pytest -m live                # hits real public venue endpoints
uv run ruff check src/ tests/ # lint
uv run mypy                   # strict type check (whole package)
```

`pytest` alone runs everything including the `live` network tests; pass
`-m "not live"` for the offline suite. All three — ruff, strict mypy over the
whole package, and the offline test suite — pass clean.

## BAML

Equivalence types, functions, clients, and golden tests live in `baml_src/*.baml`
and compile to `src/stellasaurus/baml_client/`. Tests are runnable from the BAML
VSCode playground; the pinned model is set in `baml_src/clients.baml`.
