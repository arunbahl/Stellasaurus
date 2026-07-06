"""Paper P&L records (§6.10 realized-vs-predicted tracking, paper form)."""

from __future__ import annotations

import json
from typing import Any

from stellasaurus.common.clock import wall_ms
from stellasaurus.storage.db import Database


class PnlRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    def record(
        self,
        *,
        pair_id: str,
        predicted_edge_micros: int,
        realized_edge_micros: int,
        fees_micros: int,
        detail: dict[str, Any],
    ) -> None:
        with self._db.connect() as conn:
            conn.execute(
                "INSERT INTO pnl (pair_id, ts_ms, predicted_edge_micros, "
                "realized_edge_micros, fees_micros, detail_json) VALUES (?,?,?,?,?,?)",
                (pair_id, wall_ms(), predicted_edge_micros, realized_edge_micros,
                 fees_micros, json.dumps(detail, default=str)),
            )
            conn.commit()

    def totals(self) -> dict[str, int]:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n, "
                "COALESCE(SUM(realized_edge_micros),0) AS realized, "
                "COALESCE(SUM(predicted_edge_micros),0) AS predicted, "
                "COALESCE(SUM(fees_micros),0) AS fees FROM pnl"
            ).fetchone()
        return {
            "settled": int(row["n"]),
            "realized_micros": int(row["realized"]),
            "predicted_micros": int(row["predicted"]),
            "fees_micros": int(row["fees"]),
        }
