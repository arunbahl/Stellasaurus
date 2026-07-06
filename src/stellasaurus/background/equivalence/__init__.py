"""Resolution-equivalence engine (DESIGN §6.2) — Fireworks AI behind BAML.

Background plane only; never on the trade hot path. The hot path consumes only the
resulting Verified Pair Registry.
"""

from stellasaurus.background.equivalence.engine import (
    EquivalenceEngine,
    contract_from_market,
    verdict_to_criteria,
)

__all__ = ["EquivalenceEngine", "contract_from_market", "verdict_to_criteria"]
