"""Per-stage latency metrics (DESIGN §6.11 / §8 budget component b).

GO-REWRITABLE BOUNDARY: stdlib + ``common`` only.

Two stages measured on every book update:
  * ``ingest_lag``: venue frame receipt -> evaluator invocation (queueing +
    normalization + publish overhead).
  * ``eval``: evaluator on_book_update duration (the part §8 says we fully
    control and must keep allocation-free).

Fixed-size ring buffers; snapshot() computes count/avg/p50/p95/max without
allocating on the write path.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class LatencyStats:
    stage: str
    count: int
    avg_us: float
    p50_us: float
    p95_us: float
    max_us: float


class LatencyRecorder:
    def __init__(self, size: int = 2048) -> None:
        self._size = size
        self._rings: dict[str, list[int]] = {}
        self._idx: dict[str, int] = {}
        self._count: dict[str, int] = {}
        self._lock = threading.Lock()

    def record(self, stage: str, duration_ns: int) -> None:
        with self._lock:
            ring = self._rings.get(stage)
            if ring is None:
                ring = [0] * self._size
                self._rings[stage] = ring
                self._idx[stage] = 0
                self._count[stage] = 0
            ring[self._idx[stage]] = duration_ns
            self._idx[stage] = (self._idx[stage] + 1) % self._size
            self._count[stage] += 1

    def snapshot(self) -> tuple[LatencyStats, ...]:
        with self._lock:
            out = []
            for stage, ring in self._rings.items():
                n = min(self._count[stage], self._size)
                vals = sorted(ring[:n])
                if not vals:
                    continue
                out.append(LatencyStats(
                    stage=stage,
                    count=self._count[stage],
                    avg_us=sum(vals) / n / 1000,
                    p50_us=vals[n // 2] / 1000,
                    p95_us=vals[min(n - 1, int(n * 0.95))] / 1000,
                    max_us=vals[-1] / 1000,
                ))
            return tuple(out)
