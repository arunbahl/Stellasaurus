"""Acceptance-criteria equivalence evaluation via Fireworks AI + BAML.

Types, the function, and the LLM client all live in ``baml_src/`` (idiomatic
BAML). The verdict schema is deliberately minimal — ``equivalent`` +
``outcome_polarity`` + ``reason`` — because models emit the judgment far more
reliably than a heavyweight per-dimension extraction (the four dimensions are
still checked, in the prompt).

This Python wrapper holds NO model/client config. It only:
  * builds the BAML ``Contract`` input from a catalog market, and
  * maps the verdict onto our domain types for the registry.

Fail-safe: any error or ambiguity resolves to NOT_EQUIVALENT (do not trade).
"""

from __future__ import annotations

import os
from typing import Any

from stellasaurus.baml_client.async_client import b as baml
from stellasaurus.baml_client.types import Contract, EquivalenceVerdict
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


def verdict_to_criteria(verdict: EquivalenceVerdict) -> dict[str, Any]:
    """Flatten the verdict into the JSON stored on ``pair_registry.acceptance_criteria``."""
    return {
        "equivalent": verdict.equivalent,
        "outcome_polarity": verdict.outcome_polarity.value,
        "reason": verdict.reason,
    }


class EquivalenceEngine:
    """Thin wrapper over the BAML ``EvaluateEquivalence`` function.

    Client/model config lives in ``baml_src/clients.baml``; this class carries no
    secrets. It only adds the domain mapping.
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
        try:
            verdict: EquivalenceVerdict = await baml.EvaluateEquivalence(contract_a, contract_b)
        except Exception:  # noqa: BLE001 - one retry absorbs ~20% parse flakes
            # BAML's retry_policy covers transport errors but NOT schema-parse
            # failures (model emitted unparseable output), so retry once here.
            verdict = await baml.EvaluateEquivalence(contract_a, contract_b)
        _log.info(
            "equivalence_evaluated",
            a=contract_a.native_id,
            b=contract_b.native_id,
            equivalent=verdict.equivalent,
            polarity=verdict.outcome_polarity.value,
        )
        return verdict

    @staticmethod
    def disposition(
        verdict: EquivalenceVerdict,
    ) -> tuple[PairStatus, OutcomePolarity, dict[str, Any]]:
        """Map a verdict to (registry status, polarity, acceptance_criteria JSON)."""
        status = PairStatus.VERIFIED if verdict.equivalent else PairStatus.NOT_EQUIVALENT
        polarity = OutcomePolarity(verdict.outcome_polarity.value)
        return status, polarity, verdict_to_criteria(verdict)
