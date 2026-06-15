from decimal import Decimal

import pytest

from stellasaurus.common.money import (
    cents_to_micros,
    dollars_to_micros,
    micros_to_str,
    round_to_precision,
)


def test_dollars_to_micros_exact():
    assert dollars_to_micros("1.00") == 1_000_000
    assert dollars_to_micros("0.001") == 1_000  # Polymarket min fee
    assert dollars_to_micros("0.0001") == 100  # Kalshi direct-member precision
    assert dollars_to_micros(Decimal("0.615")) == 615_000


def test_cents_to_micros():
    assert cents_to_micros(1) == 10_000
    assert cents_to_micros(99) == 990_000


def test_round_to_precision_cent():
    # $0.01 precision == 10_000 micros
    assert round_to_precision(12_499, 10_000) == 10_000
    assert round_to_precision(15_000, 10_000) == 20_000  # half-up


def test_round_to_precision_rejects_nonpositive():
    with pytest.raises(ValueError):
        round_to_precision(100, 0)


def test_micros_to_str():
    assert micros_to_str(615_000) == "$0.6150"
    assert micros_to_str(1_000_000) == "$1.0000"
