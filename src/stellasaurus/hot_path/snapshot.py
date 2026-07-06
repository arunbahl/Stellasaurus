"""Immutable in-memory snapshots + the lock-free atomic-publish primitive.

GO-REWRITABLE BOUNDARY: stdlib + ``common`` only.

The hot path reads only immutable snapshots. A background writer builds a fresh
snapshot off to the side and publishes it with a single atomic rebind. Under
CPython, a single attribute load/store is atomic w.r.t. the GIL, so readers are
lock-free and can never observe a torn object. (Go equivalent: ``atomic.Pointer``.)
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from stellasaurus.common.types import (
    Micros,
    OutcomePolarity,
    PairSource,
    PairStatus,
)


class AtomicRef[T]:
    """Single-writer / many-reader reference holding an immutable value.

    ``get`` is a lock-free single attribute read; ``publish`` is a single atomic
    rebind. Multi-step *construction* of the new value happens before ``publish``.
    """

    __slots__ = ("_value",)

    def __init__(self, value: T) -> None:
        self._value = value

    def get(self) -> T:
        return self._value

    def publish(self, value: T) -> None:
        self._value = value


@dataclass(frozen=True, slots=True)
class PairRegistryEntry:
    """A tradeable pair (DESIGN §6.3). Immutable; replaced wholesale on update."""

    pair_id: str
    canonical_proposition: str
    kalshi_ticker: str
    poly_market_slug: str
    outcome_polarity: OutcomePolarity
    status: PairStatus
    resolves_at_ms: int | None
    acceptance_criteria: Mapping[str, object] | None
    last_verified_at_ms: int
    terms_fingerprint: str
    source: PairSource


@dataclass(frozen=True, slots=True)
class RegistrySnapshot:
    """Immutable snapshot of the Verified Pair Registry.

    ``verified`` is pre-filtered to VERIFIED pair_ids so the hot path never scans.
    """

    version: int
    by_id: Mapping[str, PairRegistryEntry]
    verified: tuple[str, ...]

    @staticmethod
    def build(
        version: int, entries: list[PairRegistryEntry], *, now_ms: int | None = None
    ) -> RegistrySnapshot:
        """``verified`` excludes pairs already past resolution (when ``now_ms``
        is given) — a resolved market has no book and must not be streamed or
        evaluated, even though its registry row remains for audit."""
        by_id = {e.pair_id: e for e in entries}
        verified = tuple(
            e.pair_id
            for e in entries
            if e.status is PairStatus.VERIFIED
            and (now_ms is None or e.resolves_at_ms is None or e.resolves_at_ms > now_ms)
        )
        return RegistrySnapshot(version=version, by_id=by_id, verified=verified)

    @staticmethod
    def empty() -> RegistrySnapshot:
        return RegistrySnapshot(version=0, by_id={}, verified=())


@dataclass(frozen=True, slots=True)
class LimitsSnapshot:
    """Risk limits / [UI] params (DESIGN §6.8 / §9). Phase 1: config defaults.

    Phase 4 swaps a new snapshot atomically on each operator edit.
    """

    version: int
    halted: bool
    theta_micros: Micros
    hurdle: float
    target_size_default: int
    max_bet_value_micros: Micros
    max_bet_value_ceiling_micros: Micros
    max_aggregate_exposure_micros: Micros
    max_open_pairs: int
    max_committed_capital_micros: Micros
    min_t_days: float


@dataclass(frozen=True, slots=True)
class VenueFeedHealth:
    venue: str
    connected: bool
    transport: str  # "WS" | "REST_POLL" | "NONE"
    last_frame_ms: int | None
    frames: int
    reconnects: int


@dataclass(frozen=True, slots=True)
class FeedHealth:
    venues: tuple[VenueFeedHealth, ...] = field(default_factory=tuple)

    @staticmethod
    def empty() -> FeedHealth:
        return FeedHealth(venues=())
