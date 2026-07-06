"""Paper-opportunity records and the in-memory sink the evaluator writes to.

GO-REWRITABLE BOUNDARY: stdlib + ``common`` only.

The evaluator runs on the hot path, so it must not touch disk. It pushes
immutable ``Opportunity`` records here: the sink keeps the LATEST evaluation per
(pair, orientation) — the dashboard's live "why did/didn't it fire" table — and
a bounded history of records that WOULD have fired, which a background task
drains to the audit log.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass

from stellasaurus.common.types import Micros, Venue


@dataclass(frozen=True, slots=True)
class Opportunity:
    pair_id: str
    orientation: str  # "A" = Kalshi YES + Poly NO, "B" = Poly YES + Kalshi NO
    yes_venue: Venue
    no_venue: Venue
    would_fire: bool
    gate_failed: str | None  # first failed gate, None when would_fire
    qty: int
    vwap_yes_micros: Micros | None
    vwap_no_micros: Micros | None
    fees_per_pair_micros: Micros | None
    net_edge_micros: Micros | None  # per pair, after fees
    committed_per_pair_micros: Micros | None
    t_days: float | None
    annualized_return: float | None
    theta_micros: Micros
    hurdle: float
    created_wall_ms: int


class OpportunitySink:
    """Latest evaluation per (pair, orientation) + bounded fired history.

    Writes are cheap dict/deque ops under a lock held for nanoseconds; reads
    take the same lock briefly and copy. The evaluator is the only writer.
    """

    def __init__(self, fired_maxlen: int = 500) -> None:
        self._latest: dict[tuple[str, str], Opportunity] = {}
        self._fired: deque[Opportunity] = deque(maxlen=fired_maxlen)
        self._undrained: deque[Opportunity] = deque(maxlen=fired_maxlen)
        self._lock = threading.Lock()

    def push(self, opp: Opportunity) -> None:
        with self._lock:
            self._latest[(opp.pair_id, opp.orientation)] = opp
            if opp.would_fire:
                self._fired.append(opp)
                self._undrained.append(opp)

    def latest(self) -> tuple[Opportunity, ...]:
        with self._lock:
            return tuple(self._latest.values())

    def fired(self) -> tuple[Opportunity, ...]:
        with self._lock:
            return tuple(self._fired)

    def drain_fired(self) -> tuple[Opportunity, ...]:
        """Pop the not-yet-audited fired records (background drain task)."""
        with self._lock:
            out = tuple(self._undrained)
            self._undrained.clear()
        return out

    def prune(self, live_pair_ids: frozenset[str]) -> None:
        """Drop latest-records for pairs no longer streamed (expired/removed)."""
        with self._lock:
            for key in [k for k in self._latest if k[0] not in live_pair_ids]:
                del self._latest[key]
