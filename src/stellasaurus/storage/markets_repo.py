"""CRUD for the ``markets`` catalog table."""

from __future__ import annotations

from dataclasses import dataclass

from stellasaurus.common.clock import wall_ms
from stellasaurus.common.types import Venue
from stellasaurus.storage.db import Database


@dataclass(frozen=True, slots=True)
class MarketRow:
    venue: Venue
    native_id: str
    title: str
    rules_text: str | None
    settlement_source: str | None
    resolves_at_ms: int | None
    status: str | None
    terms_fingerprint: str
    raw_json: str | None = None


class MarketsRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    def upsert(self, market: MarketRow) -> str | None:
        """Insert or update a market. Returns the PREVIOUS terms_fingerprint if it
        existed and changed (so the caller can flag affected pairs STALE), else
        ``None``."""
        now = wall_ms()
        with self._db.connect() as conn:
            prior = conn.execute(
                "SELECT terms_fingerprint FROM markets WHERE venue=? AND native_id=?",
                (market.venue.value, market.native_id),
            ).fetchone()
            prior_fp = prior["terms_fingerprint"] if prior else None
            conn.execute(
                """
                INSERT INTO markets (venue, native_id, title, rules_text, settlement_source,
                                     resolves_at_ms, status, terms_fingerprint, raw_json,
                                     first_seen_ms, updated_ms)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(venue, native_id) DO UPDATE SET
                    title=excluded.title,
                    rules_text=excluded.rules_text,
                    settlement_source=excluded.settlement_source,
                    resolves_at_ms=excluded.resolves_at_ms,
                    status=excluded.status,
                    terms_fingerprint=excluded.terms_fingerprint,
                    raw_json=excluded.raw_json,
                    updated_ms=excluded.updated_ms
                """,
                (
                    market.venue.value,
                    market.native_id,
                    market.title,
                    market.rules_text,
                    market.settlement_source,
                    market.resolves_at_ms,
                    market.status,
                    market.terms_fingerprint,
                    market.raw_json,
                    now,
                    now,
                ),
            )
            conn.commit()
        if prior_fp is not None and prior_fp != market.terms_fingerprint:
            return str(prior_fp)
        return None

    def upsert_many(self, markets: list[MarketRow]) -> list[tuple[str, str]]:
        """Batch upsert in ONE transaction (a per-row connect/commit at catalog
        scale blocked the event loop for seconds and starved the WS feeds).
        Returns [(venue, native_id)] whose terms_fingerprint changed."""
        if not markets:
            return []
        now = wall_ms()
        changed: list[tuple[str, str]] = []
        with self._db.connect() as conn:
            for m in markets:
                prior = conn.execute(
                    "SELECT terms_fingerprint FROM markets WHERE venue=? AND native_id=?",
                    (m.venue.value, m.native_id),
                ).fetchone()
                if prior and prior["terms_fingerprint"] != m.terms_fingerprint:
                    changed.append((m.venue.value, m.native_id))
                conn.execute(
                    """
                    INSERT INTO markets (venue, native_id, title, rules_text, settlement_source,
                                         resolves_at_ms, status, terms_fingerprint, raw_json,
                                         first_seen_ms, updated_ms)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(venue, native_id) DO UPDATE SET
                        title=excluded.title, rules_text=excluded.rules_text,
                        settlement_source=excluded.settlement_source,
                        resolves_at_ms=excluded.resolves_at_ms, status=excluded.status,
                        terms_fingerprint=excluded.terms_fingerprint,
                        raw_json=excluded.raw_json, updated_ms=excluded.updated_ms
                    """,
                    (m.venue.value, m.native_id, m.title, m.rules_text,
                     m.settlement_source, m.resolves_at_ms, m.status,
                     m.terms_fingerprint, m.raw_json, now, now),
                )
            conn.commit()
        return changed

    def get(self, venue: Venue, native_id: str) -> MarketRow | None:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM markets WHERE venue=? AND native_id=?",
                (venue.value, native_id),
            ).fetchone()
        return _to_row(row) if row else None

    def unresolved_markets(self, venue: Venue, *, now_ms: int) -> list[MarketRow]:
        """Every catalogued market for a venue that has not yet resolved —
        the accumulated result of all sweeps, which is what pairing consumes."""
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM markets WHERE venue=? "
                "AND (resolves_at_ms IS NULL OR resolves_at_ms > ?)",
                (venue.value, now_ms),
            ).fetchall()
        return [_to_row(r) for r in rows]

    def near_resolution_native_ids(
        self, venue: Venue, *, now_ms: int, window_ms: int
    ) -> list[str]:
        """Native ids of markets resolving within the window (priority sweep)."""
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT native_id FROM markets WHERE venue=? "
                "AND resolves_at_ms IS NOT NULL AND resolves_at_ms > ? "
                "AND resolves_at_ms <= ?",
                (venue.value, now_ms, now_ms + window_ms),
            ).fetchall()
        return [r["native_id"] for r in rows]

    def count_by_venue(self) -> dict[str, int]:
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT venue, COUNT(*) AS n FROM markets GROUP BY venue"
            ).fetchall()
        return {r["venue"]: r["n"] for r in rows}


def _to_row(row: object) -> MarketRow:
    r = row  # sqlite3.Row
    return MarketRow(
        venue=Venue(r["venue"]),  # type: ignore[index]
        native_id=r["native_id"],  # type: ignore[index]
        title=r["title"],  # type: ignore[index]
        rules_text=r["rules_text"],  # type: ignore[index]
        settlement_source=r["settlement_source"],  # type: ignore[index]
        resolves_at_ms=r["resolves_at_ms"],  # type: ignore[index]
        status=r["status"],  # type: ignore[index]
        terms_fingerprint=r["terms_fingerprint"],  # type: ignore[index]
        raw_json=r["raw_json"],  # type: ignore[index]
    )
