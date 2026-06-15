"""Append-only audit log (DESIGN §6.11). Implements the ``_AuditSink`` shape used
by ``common.logging.audit``."""

from __future__ import annotations

import json
from typing import Any

from stellasaurus.common.clock import wall_ms
from stellasaurus.storage.db import Database


class AuditRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    def append(
        self, *, actor: str, event_type: str, pair_id: str | None, detail: dict[str, Any]
    ) -> None:
        with self._db.connect() as conn:
            conn.execute(
                "INSERT INTO audit_log (ts_ms, actor, event_type, pair_id, detail_json) "
                "VALUES (?,?,?,?,?)",
                (wall_ms(), actor, event_type, pair_id, json.dumps(detail, default=str)),
            )
            conn.commit()

    def recent(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT ts_ms, actor, event_type, pair_id, detail_json "
                "FROM audit_log ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {
                "ts_ms": r["ts_ms"],
                "actor": r["actor"],
                "event_type": r["event_type"],
                "pair_id": r["pair_id"],
                "detail": json.loads(r["detail_json"]),
            }
            for r in rows
        ]
