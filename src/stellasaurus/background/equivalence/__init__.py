"""Resolution-equivalence engine (DESIGN §6.2) — Fireworks AI behind BAML.

Background plane only; never on the trade hot path. The hot path consumes only the
resulting Verified Pair Registry.
"""

from stellasaurus.background.equivalence.engine import (
    EquivalenceEngine,
    all_dimensions_match,
    contract_from_market,
)

__all__ = ["EquivalenceEngine", "all_dimensions_match", "contract_from_market"]
