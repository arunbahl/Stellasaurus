"""FastAPI dashboard: REST read endpoints + a WebSocket push of live state.

The dashboard is a pure read model over the hot-path snapshots; it never mutates
hot state (control actions arrive in Phase 4).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from stellasaurus.common.logging import get_logger
from stellasaurus.control.readmodel import ReadModel

_log = get_logger("control.app")
_STATIC = Path(__file__).parent / "static"


def create_app(read_model: ReadModel, *, push_interval_ms: int = 250) -> FastAPI:
    app = FastAPI(title="Stellasaurus — Phase 1 (read-only spine)")

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return (_STATIC / "index.html").read_text("utf-8")

    @app.get("/health")
    async def health() -> dict:
        return read_model.health()

    @app.get("/pairs")
    async def pairs() -> list:
        return read_model.pairs()

    @app.get("/catalog/stats")
    async def catalog_stats() -> dict:
        return read_model.catalog_stats()

    @app.get("/books")
    async def books() -> list:
        return read_model.all_book_views()

    @app.get("/opportunities")
    async def opportunities() -> dict:
        return read_model.opportunities()

    @app.get("/books/{pair_id}")
    async def book(pair_id: str) -> dict:
        return read_model.book_view(pair_id)

    @app.websocket("/ws")
    async def ws(socket: WebSocket) -> None:
        await socket.accept()
        interval = push_interval_ms / 1000.0
        try:
            while True:
                payload = {
                    "health": read_model.health(),
                    "pairs": read_model.pairs(),
                    "catalog": read_model.catalog_stats(),
                    "books": read_model.all_book_views(),
                    "opportunities": read_model.opportunities(),
                }
                await socket.send_text(json.dumps(payload, default=str))
                await asyncio.sleep(interval)
        except WebSocketDisconnect:
            return
        except Exception as exc:  # noqa: BLE001
            _log.warning("ws_push_error", error=str(exc))

    if _STATIC.exists():
        app.mount("/static", StaticFiles(directory=_STATIC), name="static")

    return app
