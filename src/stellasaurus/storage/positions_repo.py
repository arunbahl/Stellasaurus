"""Persist paper positions (two leg rows per position, shared prefix id)."""

from __future__ import annotations

import json

from stellasaurus.common.types import Side
from stellasaurus.hot_path.positions import PaperPosition
from stellasaurus.storage.db import Database


class PositionsRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    def upsert(self, p: PaperPosition) -> None:
        shared = {
            "orientation": p.orientation,
            "hedge_status": p.hedge_status.value,
            "fees_micros": p.fees_micros,
            "committed_micros": p.committed_micros,
            "unwind_loss_micros": p.unwind_loss_micros,
            "resolves_at_ms": p.resolves_at_ms,
        }
        legs = [
            (f"{p.position_id}-yes", p.yes_venue.value, Side.YES.value, p.yes_price_micros),
        ]
        if p.no_price_micros is not None:
            legs.append(
                (f"{p.position_id}-no", p.no_venue.value, Side.NO.value, p.no_price_micros)
            )
        with self._db.connect() as conn:
            for leg_id, venue, side, price in legs:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO positions
                        (position_id, pair_id, venue, native_id, side, qty,
                         avg_price_micros, opened_ms, closed_ms, hedge_status, raw_json)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        leg_id, p.pair_id, venue, p.pair_id, side, p.qty,
                        price, p.opened_wall_ms, None, p.hedge_status.value,
                        json.dumps(shared),
                    ),
                )
            conn.commit()

    def count(self) -> int:
        with self._db.connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM positions").fetchone()
        return int(row["n"])
