"""CRUD for the ``pair_registry`` table; builds hot-path registry entries."""

from __future__ import annotations

import json

from stellasaurus.common.clock import wall_ms
from stellasaurus.common.types import OutcomePolarity, PairSource, PairStatus
from stellasaurus.hot_path.snapshot import PairRegistryEntry
from stellasaurus.storage.db import Database


class RegistryRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    def upsert(self, entry: PairRegistryEntry) -> None:
        now = wall_ms()
        criteria = json.dumps(entry.acceptance_criteria) if entry.acceptance_criteria else None
        with self._db.connect() as conn:
            conn.execute(
                """
                INSERT INTO pair_registry (pair_id, canonical_proposition, kalshi_ticker,
                    poly_market_slug, outcome_polarity, status, resolves_at_ms,
                    acceptance_criteria, terms_fingerprint, last_verified_at_ms, source, updated_ms)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(pair_id) DO UPDATE SET
                    canonical_proposition=excluded.canonical_proposition,
                    kalshi_ticker=excluded.kalshi_ticker,
                    poly_market_slug=excluded.poly_market_slug,
                    outcome_polarity=excluded.outcome_polarity,
                    status=excluded.status,
                    resolves_at_ms=excluded.resolves_at_ms,
                    acceptance_criteria=excluded.acceptance_criteria,
                    terms_fingerprint=excluded.terms_fingerprint,
                    last_verified_at_ms=excluded.last_verified_at_ms,
                    source=excluded.source,
                    updated_ms=excluded.updated_ms
                """,
                (
                    entry.pair_id,
                    entry.canonical_proposition,
                    entry.kalshi_ticker,
                    entry.poly_market_slug,
                    entry.outcome_polarity.value,
                    entry.status.value,
                    entry.resolves_at_ms,
                    criteria,
                    entry.terms_fingerprint,
                    entry.last_verified_at_ms,
                    entry.source.value,
                    now,
                ),
            )
            conn.commit()

    def set_status(self, pair_id: str, status: PairStatus) -> None:
        with self._db.connect() as conn:
            conn.execute(
                "UPDATE pair_registry SET status=?, updated_ms=? WHERE pair_id=?",
                (status.value, wall_ms(), pair_id),
            )
            conn.commit()

    def all_entries(self) -> list[PairRegistryEntry]:
        with self._db.connect() as conn:
            rows = conn.execute("SELECT * FROM pair_registry").fetchall()
        return [_to_entry(r) for r in rows]

    def find_by_legs(self, kalshi_ticker: str, poly_slug: str) -> str | None:
        """pair_id of an existing entry with exactly these two legs, else None."""
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT pair_id FROM pair_registry WHERE kalshi_ticker=? AND poly_market_slug=?",
                (kalshi_ticker, poly_slug),
            ).fetchone()
        return row["pair_id"] if row else None

    def pairs_referencing(self, *, kalshi_ticker: str | None, poly_slug: str | None) -> list[str]:
        """pair_ids that reference a given native market (for STALE propagation)."""
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT pair_id FROM pair_registry WHERE kalshi_ticker=? OR poly_market_slug=?",
                (kalshi_ticker or "", poly_slug or ""),
            ).fetchall()
        return [r["pair_id"] for r in rows]


def _to_entry(r: object) -> PairRegistryEntry:
    criteria = r["acceptance_criteria"]  # type: ignore[index]
    return PairRegistryEntry(
        pair_id=r["pair_id"],  # type: ignore[index]
        canonical_proposition=r["canonical_proposition"],  # type: ignore[index]
        kalshi_ticker=r["kalshi_ticker"],  # type: ignore[index]
        poly_market_slug=r["poly_market_slug"],  # type: ignore[index]
        outcome_polarity=OutcomePolarity(r["outcome_polarity"]),  # type: ignore[index]
        status=PairStatus(r["status"]),  # type: ignore[index]
        resolves_at_ms=r["resolves_at_ms"],  # type: ignore[index]
        acceptance_criteria=json.loads(criteria) if criteria else None,
        last_verified_at_ms=r["last_verified_at_ms"],  # type: ignore[index]
        terms_fingerprint=r["terms_fingerprint"] or "",  # type: ignore[index]
        source=PairSource(r["source"]),  # type: ignore[index]
    )
