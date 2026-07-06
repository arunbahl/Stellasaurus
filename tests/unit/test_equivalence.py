"""Equivalence engine wrapper — simplified-verdict mapping (no network)."""

import pytest

from stellasaurus.background.equivalence import EquivalenceEngine, contract_from_market
from stellasaurus.baml_client.types import (
    Contract,
    EquivalenceVerdict,
    OutcomePolarity,
)
from stellasaurus.common.types import OutcomePolarity as DomainPolarity
from stellasaurus.common.types import PairStatus


def _verdict(*, equivalent: bool, polarity=OutcomePolarity.DIRECT) -> EquivalenceVerdict:
    return EquivalenceVerdict(
        equivalent=equivalent, outcome_polarity=polarity, reason="because"
    )


def test_equivalent_maps_to_verified():
    status, polarity, criteria = EquivalenceEngine.disposition(_verdict(equivalent=True))
    assert status is PairStatus.VERIFIED
    assert polarity is DomainPolarity.DIRECT
    assert criteria == {"equivalent": True, "outcome_polarity": "DIRECT", "reason": "because"}


def test_not_equivalent_maps_to_not_equivalent():
    status, _, criteria = EquivalenceEngine.disposition(_verdict(equivalent=False))
    assert status is PairStatus.NOT_EQUIVALENT
    assert criteria["equivalent"] is False


def test_inverted_polarity_maps_through():
    _, polarity, _ = EquivalenceEngine.disposition(
        _verdict(equivalent=True, polarity=OutcomePolarity.INVERTED)
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
    assert c.settlement_source == "Official"


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
