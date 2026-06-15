"""Acceptance-criteria equivalence evaluation via Fireworks AI + BAML.

Types, the function, and the LLM client all live in ``baml_src/`` (idiomatic
BAML): the ``Fireworks`` client reads ``FIREWORKS_LLM_ENDPOINT`` and
``FIREWORKS_API_KEY_BAML`` from the environment, and ``EvaluateEquivalence``
extracts + compares the two contracts' acceptance criteria, returning a typed
``EquivalenceVerdict`` (DESIGN §6.2).

This Python wrapper holds NO model/client config. It only:
  * builds the BAML ``Contract`` input from a catalog market,
  * enforces the DESIGN §6.2 rule ourselves — equivalent ONLY if every dimension
    matches (never trusting the model's ``equivalent`` flag alone), and
  * maps the verdict onto our domain types for the registry.

Fail-safe: any uncertainty resolves to NOT_EQUIVALENT (do not trade).
"""

from __future__ import annotations

import os
from typing import Any

from stellasaurus.baml_client.async_client import b as baml
from stellasaurus.baml_client.types import Contract, DimensionMatch, EquivalenceVerdict
from stellasaurus.common.logging import get_logger
from stellasaurus.common.types import OutcomePolarity, PairStatus

_log = get_logger("background.equivalence")


def contract_from_market(market: Any) -> Contract:
    """Build the BAML ``Contract`` input from anything exposing the catalog fields
    (RawMarket / MarketRow)."""
    return Contract(
        native_id=market.native_id,
        title=market.title,
        rules_text=market.rules_text,
        settlement_source=market.settlement_source,
        resolves_at_ms=market.resolves_at_ms,
    )


def all_dimensions_match(dm: DimensionMatch) -> bool:
    return bool(
        dm.proposition and dm.settlement_source and dm.timing_cutoff and dm.edge_case_rules
    )


def verdict_is_equivalent(verdict: EquivalenceVerdict) -> bool:
    """Conservative conjunction: every dimension must match AND the model must
    also conclude equivalent. Any disagreement -> not equivalent (fail-safe)."""
    return all_dimensions_match(verdict.dimension_match) and verdict.equivalent


def verdict_to_criteria(verdict: EquivalenceVerdict) -> dict[str, Any]:
    """Flatten the verdict into the JSON stored on ``pair_registry.acceptance_criteria``."""

    def crit(c: Any) -> dict[str, str]:
        return {
            "proposition": c.proposition,
            "settlement_source": c.settlement_source,
            "timing_cutoff": c.timing_cutoff,
            "edge_case_rules": c.edge_case_rules,
        }

    dm = verdict.dimension_match
    return {
        "contract_a_criteria": crit(verdict.contract_a_criteria),
        "contract_b_criteria": crit(verdict.contract_b_criteria),
        "dimension_match": {
            "proposition": dm.proposition,
            "settlement_source": dm.settlement_source,
            "timing_cutoff": dm.timing_cutoff,
            "edge_case_rules": dm.edge_case_rules,
        },
        "outcome_polarity": verdict.outcome_polarity.value,
        "rationale": verdict.rationale,
    }


class EquivalenceEngine:
    """Thin wrapper over the BAML ``EvaluateEquivalence`` function.

    Client/model config lives in ``baml_src/clients.baml``; this class carries no
    secrets. It only adds the domain mapping and the conservative safeguard.
    """

    @property
    def configured(self) -> bool:
        """The BAML Fireworks client needs its key in the environment."""
        return bool(os.environ.get("FIREWORKS_API_KEY_BAML") or os.environ.get("FIREWORKS_API_KEY"))

    async def evaluate(self, contract_a: Contract, contract_b: Contract) -> EquivalenceVerdict:
        """Run the acceptance-criteria comparison. Raises if no API key is set."""
        if not self.configured:
            raise RuntimeError(
                "Fireworks API key not configured "
                "(set FIREWORKS_API_KEY_BAML in the environment or .env)"
            )
        verdict: EquivalenceVerdict = await baml.EvaluateEquivalence(contract_a, contract_b)
        _log.info(
            "equivalence_evaluated",
            a=contract_a.native_id,
            b=contract_b.native_id,
            equivalent=verdict_is_equivalent(verdict),
            polarity=verdict.outcome_polarity.value,
        )
        return verdict

    @staticmethod
    def disposition(
        verdict: EquivalenceVerdict,
    ) -> tuple[PairStatus, OutcomePolarity, dict[str, Any]]:
        """Map a verdict to (registry status, polarity, acceptance_criteria JSON)."""
        status = (
            PairStatus.VERIFIED if verdict_is_equivalent(verdict) else PairStatus.NOT_EQUIVALENT
        )
        polarity = OutcomePolarity(verdict.outcome_polarity.value)
        return status, polarity, verdict_to_criteria(verdict)
