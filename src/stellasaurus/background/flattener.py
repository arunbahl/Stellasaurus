"""Automated naked-leg flattener (safety net for HANGING legs).

When the inline single-leg unwind in ``LiveExecutionEngine`` cannot complete
(e.g. the offsetting side has no resting volume at that instant), a leg is left
NAKED — pure directional risk the design forbids. Rather than depend on a human
to notice and flatten, this task OWNS the naked leg: it reads the authoritative
position from the venue and issues marketable IOC closes at escalating
aggressiveness over a time budget, re-verifying against the venue until the
position is genuinely flat.

Policy (deliberate): flattening the RISK is automated; RESUME is not. A hang
means an expected fill didn't happen — the system stays halted for new entries
until a human reviews why and clears it. This removes lingering exposure without
risking blind re-entry.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from stellasaurus.common.clock import Clock, SystemClock
from stellasaurus.common.logging import get_logger
from stellasaurus.common.types import Venue

_log = get_logger("background.flattener")


@dataclass(frozen=True, slots=True)
class NakedLeg:
    venue: Venue
    native_id: str


class _Closer:
    """Structural type for the gateway methods the flattener needs."""

    async def net_position(self, native_id: str) -> int: ...  # noqa: D102
    async def close_position(self, native_id: str) -> int: ...  # noqa: D102


class PositionFlattener:
    def __init__(
        self,
        *,
        gateways: dict[Venue, _Closer],
        max_attempts: int = 8,
        backoff_seconds: float = 2.0,
        clock: Clock | None = None,
    ) -> None:
        self._gateways = gateways
        self._max_attempts = max_attempts
        self._backoff = backoff_seconds
        self._clock = clock or SystemClock()
        self._queue: asyncio.Queue[NakedLeg] = asyncio.Queue()

    def enqueue(self, leg: NakedLeg) -> None:
        """Non-blocking hand-off from the hot path's unwind branch."""
        self._queue.put_nowait(leg)

    async def run(self) -> None:
        while True:
            leg = await self._queue.get()
            try:
                await self.flatten(leg)
            except Exception as exc:  # noqa: BLE001 - never kill the worker
                _log.error("flatten_worker_error", venue=leg.venue.value,
                           native_id=leg.native_id, error=str(exc))

    async def flatten(self, leg: NakedLeg) -> bool:
        """Drive the leg to flat, re-verifying against the venue. Returns True
        once net == 0; False if the attempt budget is exhausted (still halted,
        alert stands)."""
        gw = self._gateways.get(leg.venue)
        if gw is None:
            _log.error("flatten_no_gateway", venue=leg.venue.value)
            return False
        for attempt in range(1, self._max_attempts + 1):
            residual = await gw.close_position(leg.native_id)
            if residual == 0:
                _log.warning("flatten_success", venue=leg.venue.value,
                             native_id=leg.native_id, attempts=attempt)
                return True
            _log.warning("flatten_residual", venue=leg.venue.value,
                         native_id=leg.native_id, residual=residual, attempt=attempt)
            if attempt < self._max_attempts:
                await asyncio.sleep(self._backoff)
        _log.error("flatten_EXHAUSTED_still_naked", venue=leg.venue.value,
                   native_id=leg.native_id)
        return False
