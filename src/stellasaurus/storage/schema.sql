-- Stellasaurus durable store (SQLite). Source of truth for the pair registry,
-- catalog, audit log, and (Phase 3+) positions / P&L. The hot path never reads
-- this file directly — it consumes in-memory snapshots published in front of it.

PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version    INTEGER PRIMARY KEY,
    applied_ms INTEGER NOT NULL
);

-- One row per tradeable market on either venue, refreshed by catalog sync.
CREATE TABLE IF NOT EXISTS markets (
    venue             TEXT    NOT NULL,          -- 'KALSHI' | 'POLYMARKET'
    native_id         TEXT    NOT NULL,          -- kalshi ticker | poly slug
    title             TEXT    NOT NULL,
    rules_text        TEXT,                      -- subtitle / resolution rules
    settlement_source TEXT,
    resolves_at_ms    INTEGER,                   -- epoch ms
    status            TEXT,                      -- venue market status
    terms_fingerprint TEXT    NOT NULL,          -- of acceptance-criteria fields
    raw_json          TEXT,                      -- last raw catalog payload (debug)
    first_seen_ms     INTEGER NOT NULL,
    updated_ms        INTEGER NOT NULL,
    PRIMARY KEY (venue, native_id)
);
CREATE INDEX IF NOT EXISTS idx_markets_resolves ON markets(resolves_at_ms);

-- The Verified Pair Registry (DESIGN §6.3). Source of truth; an immutable
-- in-memory snapshot is published to the hot path on each change.
CREATE TABLE IF NOT EXISTS pair_registry (
    pair_id               TEXT    PRIMARY KEY,
    canonical_proposition TEXT    NOT NULL,
    kalshi_ticker         TEXT    NOT NULL,
    poly_market_slug      TEXT    NOT NULL,
    outcome_polarity      TEXT    NOT NULL,      -- 'DIRECT' | 'INVERTED'
    status                TEXT    NOT NULL,      -- 'VERIFIED' | 'NOT_EQUIVALENT' | 'STALE'
    resolves_at_ms        INTEGER,
    acceptance_criteria   TEXT,                  -- JSON; Phase-1 note, Phase-2 LLM output
    terms_fingerprint     TEXT,                  -- joined markets fp at verify time
    last_verified_at_ms   INTEGER NOT NULL,
    source                TEXT    NOT NULL,      -- 'MANUAL_SEED' | 'LLM'
    updated_ms            INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_registry_status ON pair_registry(status);

-- Append-only decision/event record (DESIGN §6.11).
CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms       INTEGER NOT NULL,
    actor       TEXT    NOT NULL,                -- 'system' | 'catalog_sync' | operator
    event_type  TEXT    NOT NULL,
    pair_id     TEXT,
    detail_json TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts_ms);

-- Phase 3+ stubs, created now so later migrations don't churn the spine.
CREATE TABLE IF NOT EXISTS positions (
    position_id     TEXT PRIMARY KEY,
    pair_id         TEXT REFERENCES pair_registry(pair_id),
    venue           TEXT,
    native_id       TEXT,
    side            TEXT,
    qty             INTEGER,
    avg_price_micros INTEGER,
    opened_ms       INTEGER,
    closed_ms       INTEGER,
    hedge_status    TEXT,                        -- 'HEDGED' | 'UNHEDGED' | 'FLAT'
    raw_json        TEXT
);

CREATE TABLE IF NOT EXISTS pnl (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    pair_id               TEXT,
    ts_ms                 INTEGER,
    predicted_edge_micros INTEGER,
    realized_edge_micros  INTEGER,
    fees_micros           INTEGER,
    detail_json           TEXT
);
