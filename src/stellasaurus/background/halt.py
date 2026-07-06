"""Kill switch + runtime-settable limits (DESIGN §6.9).

Manual: the dashboard POSTs halt/resume and limit changes here. Automatic: a
watcher task trips the halt flag on fail-safe conditions. Un-halting is always
manual.

Limit changes follow the §6.9 safe-propagation path: validate (numeric, >= 0),
clamp to the non-UI ceiling (max_bet_value <= max_bet_value_ceiling), publish a
new immutable LimitsSnapshot the hot path swaps in atomically, and audit-log the
old and new values. Invalid values are rejected and the prior value retained.

Auto-triggers (v1):
  * all verified pairs' books stale beyond ``auto_halt_stale_seconds`` while
    pairs exist (feed loss — you cannot assert "locked" on stale books);
  * a pair with an OPEN position leaving the verified set (terms changed or
    re-verification reversal, §10).
"""

from __future__ import annotations

from dataclasses import replace

from stellasaurus.common.clock import Clock, SystemClock
from stellasaurus.common.logging import audit, get_logger
from stellasaurus.hot_path.positions import PositionsStore
from stellasaurus.hot_path.state import HotStateStore
from stellasaurus.storage.audit_repo import AuditRepo

_log = get_logger("background.halt")

# [UI]-settable fields (§6.9) and their LimitsSnapshot attributes.
UI_LIMIT_FIELDS = frozenset({
    "theta_micros",
    "hurdle",
    "target_size_default",
    "max_bet_value_micros",
    "max_aggregate_exposure_micros",
    "max_open_pairs",
    "max_committed_capital_micros",
})
_INT_FIELDS = UI_LIMIT_FIELDS - {"hurdle"}


class HaltController:
    def __init__(
        self,
        *,
        store: HotStateStore,
        positions: PositionsStore,
        audit_repo: AuditRepo,
        auto_halt_stale_seconds: float = 60.0,
        clock: Clock | None = None,
    ) -> None:
        self._store = store
        self._positions = positions
        self._audit = audit_repo
        self._stale_after_s = auto_halt_stale_seconds
        self._clock = clock or SystemClock()
        self._all_stale_since_ms: int | None = None

    # --- manual + automatic halt ---

    def set_halted(self, halted: bool, *, actor: str, reason: str) -> None:
        limits = self._store.limits()
        if limits.halted == halted:
            return
        self._store.publish_limits(
            replace(limits, version=limits.version + 1, halted=halted)
        )
        audit(self._audit, actor=actor, event_type="HALT_SET",
              halted=halted, reason=reason)
        _log.warning("halt_changed", halted=halted, actor=actor, reason=reason)

    # --- [UI] limits (§6.9 safe propagation) ---

    def update_limits(self, changes: dict[str, object], *, actor: str) -> dict[str, str]:
        """Apply valid changes; returns {field: error} for rejected ones."""
        limits = self._store.limits()
        errors: dict[str, str] = {}
        applied: dict[str, object] = {}
        for field, value in changes.items():
            if field not in UI_LIMIT_FIELDS:
                errors[field] = "not a UI-settable limit"
                continue
            try:
                num = int(value) if field in _INT_FIELDS else float(value)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                errors[field] = "not numeric"
                continue
            if num < 0:
                errors[field] = "must be >= 0"
                continue
            if field == "max_bet_value_micros":
                num = min(int(num), limits.max_bet_value_ceiling_micros)
            applied[field] = num
        if applied:
            old = {f: getattr(limits, f) for f in applied}
            self._store.publish_limits(
                replace(limits, version=limits.version + 1, **applied)  # type: ignore[arg-type]
            )
            audit(self._audit, actor=actor, event_type="LIMITS_CHANGED",
                  old=old, new=applied)
        return errors

    # --- automatic triggers (periodic watcher job) ---

    async def watch_once(self) -> None:
        limits = self._store.limits()
        registry = self._store.registry()
        now = self._clock.wall_ms()

        # Trigger 1: every verified pair stale (feed loss / venue outage).
        if registry.verified and not limits.halted:
            if any(self._store.is_fresh(pid) for pid in registry.verified):
                self._all_stale_since_ms = None
            elif self._all_stale_since_ms is None:
                self._all_stale_since_ms = now
            elif now - self._all_stale_since_ms > self._stale_after_s * 1000:
                self.set_halted(
                    True, actor="auto",
                    reason=f"all verified pairs stale > {self._stale_after_s}s",
                )
        else:
            self._all_stale_since_ms = None

        # Trigger 2: an open position's pair left the verified set.
        if not limits.halted:
            verified = set(registry.verified)
            for p in self._positions.open_positions():
                if p.hedge_status.value == "HEDGED" and p.pair_id not in verified:
                    entry = registry.by_id.get(p.pair_id)
                    # Expired pairs resolve naturally — only halt on status flips.
                    if entry is not None and (
                        entry.resolves_at_ms is None or entry.resolves_at_ms > now
                    ):
                        self.set_halted(
                            True, actor="auto",
                            reason=f"open position pair no longer verified: {p.pair_id}",
                        )
                        break