"""SQLite connection management + schema bootstrap.

Single-file local store. WAL mode lets the dashboard read while the background
plane writes. Connections are short-lived and created per-operation by the repos;
``check_same_thread=False`` is safe because we serialize writes through the
single asyncio loop / background tasks.
"""

from __future__ import annotations

import sqlite3
from importlib import resources
from pathlib import Path

from stellasaurus.common.clock import wall_ms
from stellasaurus.common.logging import get_logger

_log = get_logger("storage.db")
_SCHEMA_VERSION = 1


def _read_schema() -> str:
    return resources.files("stellasaurus.storage").joinpath("schema.sql").read_text("utf-8")


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


class Database:
    """Owns the db path and applies the schema on first use."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    def connect(self) -> sqlite3.Connection:
        return connect(self.db_path)

    def migrate(self) -> None:
        with self.connect() as conn:
            conn.executescript(_read_schema())
            row = conn.execute("SELECT MAX(version) AS v FROM schema_migrations").fetchone()
            current = row["v"] if row and row["v"] is not None else 0
            if current < _SCHEMA_VERSION:
                conn.execute(
                    "INSERT OR REPLACE INTO schema_migrations(version, applied_ms) VALUES (?, ?)",
                    (_SCHEMA_VERSION, wall_ms()),
                )
                conn.commit()
                _log.info("schema_migrated", from_version=current, to_version=_SCHEMA_VERSION)
