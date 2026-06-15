"""Subscription sharding to respect per-venue connection limits.

Polymarket: max 100 markets per WS connection.
Kalshi:     max 5 WS connections per user (one subscription list per connection).

Both reduce to: split N native ids into <= ``max_conns`` shards, each <=
``max_per_conn`` ids.
"""

from __future__ import annotations

from collections.abc import Sequence


def shard(ids: Sequence[str], *, max_per_conn: int, max_conns: int) -> list[list[str]]:
    """Split ``ids`` into chunks of at most ``max_per_conn``, capped at
    ``max_conns`` shards. Raises if the ids cannot fit within the budget."""
    if max_per_conn <= 0 or max_conns <= 0:
        raise ValueError("limits must be positive")
    capacity = max_per_conn * max_conns
    if len(ids) > capacity:
        raise ValueError(
            f"{len(ids)} markets exceed subscription capacity "
            f"({max_conns} conns x {max_per_conn} = {capacity})"
        )
    return [list(ids[i : i + max_per_conn]) for i in range(0, len(ids), max_per_conn)] or [[]]
