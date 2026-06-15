# Stellasaurus

Locked cross-venue arbitrage system for binary event contracts on **Kalshi** and
**Polymarket US**. See [`DESIGN.md`](./DESIGN.md) for the full system design.

> **Status: Phase 1 — read-only spine.** Dual market-data ingestion, normalized
> canonical-YES order books, catalog sync, a manually-seeded Verified Pair
> Registry, and a dashboard read model. **No trading, no order execution.**

## Architecture (three planes)

- **Hot path** (`stellasaurus.hot_path`) — pure, dependency-free in-memory state:
  normalized books, immutable snapshots, freshness. The one boundary that could be
  re-implemented in Go later. Reads only pre-loaded memory; no network/disk/LLM.
- **Background plane** (`stellasaurus.background`, `stellasaurus.venues`,
  `stellasaurus.storage`) — slow work: catalog sync, registry loading, market-data
  ingestion, SQLite persistence.
- **Control plane** (`stellasaurus.control`) — FastAPI dashboard read model.

## Quick start

```bash
uv venv --python 3.12
source .venv/bin/activate
uv pip install -e ".[bg,control,dev]"

# Run the read-only spine (keyless; uses public market data)
python -m stellasaurus.app
# open http://127.0.0.1:8770
```

Seed tradeable pairs by editing [`seeds/pairs.seed.yaml`](./seeds/pairs.seed.yaml).
In Phase 1 these are **human-asserted** equivalent pairs (the LLM equivalence
engine arrives in Phase 2).

## Tests

```bash
pytest -m "not live"      # fast, offline (unit + replay)
pytest -m live            # hits real public venue endpoints
```

## Configuration

Settings load from `config/default.toml`, overridable via `STELLA_*` environment
variables (see `stellasaurus.common.config`). Runs **keyless** against public
market data by default; venue API credentials are optional in Phase 1 and only
enable authenticated WebSocket streams.
