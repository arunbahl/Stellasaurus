"""Equivalence engine wrapper — mapping + conservative conjunction (no network)."""

import pytest

from stellasaurus.background.equivalence import EquivalenceEngine, contract_from_market
from stellasaurus.baml_client.types import (
    Contract,
    ContractCriteria,
    DimensionMatch,
    EquivalenceVerdict,
    OutcomePolarity,
)
from stellasaurus.common.types import OutcomePolarity as DomainPolarity
from stellasaurus.common.types import PairStatus


def _criteria(p="prop") -> ContractCriteria:
    return ContractCriteria(
        proposition=p, settlement_source="src", timing_cutoff="cut", edge_case_rules="void"
    )


def _verdict(
    *, dims: bool, equivalent: bool, polarity=OutcomePolarity.DIRECT
) -> EquivalenceVerdict:
    return EquivalenceVerdict(
        contract_a_criteria=_criteria(),
        contract_b_criteria=_criteria(),
        dimension_match=DimensionMatch(
            proposition=dims, settlement_source=dims, timing_cutoff=dims, edge_case_rules=dims
        ),
        equivalent=equivalent,
        outcome_polarity=polarity,
        rationale="because",
    )


def test_all_dims_true_and_equivalent_flag_is_verified():
    status, polarity, criteria = EquivalenceEngine.disposition(_verdict(dims=True, equivalent=True))
    assert status is PairStatus.VERIFIED
    assert polarity is DomainPolarity.DIRECT
    assert criteria["dimension_match"]["proposition"] is True
    assert criteria["outcome_polarity"] == "DIRECT"


def test_one_dim_false_is_not_equivalent_even_if_model_says_equivalent():
    # Conservative: a single dimension mismatch overrides the model's equivalent flag.
    v = EquivalenceVerdict(
        contract_a_criteria=_criteria(),
        contract_b_criteria=_criteria(),
        dimension_match=DimensionMatch(
            proposition=True, settlement_source=False, timing_cutoff=True, edge_case_rules=True
        ),
        equivalent=True,
        outcome_polarity=OutcomePolarity.DIRECT,
        rationale="model over-claimed",
    )
    status, _, _ = EquivalenceEngine.disposition(v)
    assert status is PairStatus.NOT_EQUIVALENT


def test_model_equivalent_false_overrides_all_dims_true():
    status, _, _ = EquivalenceEngine.disposition(_verdict(dims=True, equivalent=False))
    assert status is PairStatus.NOT_EQUIVALENT


def test_inverted_polarity_maps_through():
    _, polarity, _ = EquivalenceEngine.disposition(
        _verdict(dims=True, equivalent=True, polarity=OutcomePolarity.INVERTED)
    )
    assert polarity is DomainPolarity.INVERTED


def test_contract_from_market_builds_baml_contract():
    class M:
        native_id = "KX1"
        title = "Will X happen?"
        rules_text = "Resolves YES if X."
        settlement_source = "Official"
        resolves_at_ms = 1_900_000_000_000

    c = contract_from_market(M())
    assert isinstance(c, Contract)
    assert c.native_id == "KX1"
    assert c.title == "Will X happen?"
    assert c.settlement_source == "Official"
    assert c.resolves_at_ms == 1_900_000_000_000


def test_engine_unconfigured_without_key(monkeypatch):
    monkeypatch.delenv("FIREWORKS_API_KEY", raising=False)
    monkeypatch.delenv("FIREWORKS_API_KEY_BAML", raising=False)
    assert EquivalenceEngine().configured is False


async def test_evaluate_raises_without_key(monkeypatch):
    monkeypatch.delenv("FIREWORKS_API_KEY", raising=False)
    monkeypatch.delenv("FIREWORKS_API_KEY_BAML", raising=False)
    a = Contract(
        native_id="KX1", title="t", rules_text=None, settlement_source=None, resolves_at_ms=None
    )
    with pytest.raises(RuntimeError):
        await EquivalenceEngine().evaluate(a, a)
