"""Fee-param sync: per-instrument overrides, drift audit, divergence halt."""

from decimal import Decimal

from stellasaurus.hot_path.fees import (
    FeeParams,
    kalshi_fee_micros,
    poly_fee_micros,
    reference_fee_delta_micros,
)

BASE = FeeParams(
    kalshi_taker_multiplier=Decimal("0.07"),
    kalshi_maker_multiplier=Decimal("0.0175"),
    kalshi_precision_micros=10_000,
    poly_taker_coefficient=Decimal("0.06"),
    poly_maker_coefficient=Decimal("-0.0125"),
)


def test_series_override_applies_only_to_that_series():
    params = FeeParams(
        kalshi_taker_multiplier=Decimal("0.07"),
        kalshi_maker_multiplier=Decimal("0.0175"),
        kalshi_precision_micros=10_000,
        poly_taker_coefficient=Decimal("0.06"),
        poly_maker_coefficient=Decimal("-0.0125"),
        kalshi_series_multipliers={"KXSPECIAL": Decimal("0.035")},
    )
    # override series: 0.035 * 100 * 0.25 = $0.875 -> ceil $0.88
    assert kalshi_fee_micros(100, 500_000, params=params, series="KXSPECIAL") == 880_000
    # other series keep the baseline $1.75
    assert kalshi_fee_micros(100, 500_000, params=params, series="KXHIGHNY") == 1_750_000


def test_poly_market_coefficient_override():
    params = FeeParams(
        kalshi_taker_multiplier=Decimal("0.07"),
        kalshi_maker_multiplier=Decimal("0.0175"),
        kalshi_precision_micros=10_000,
        poly_taker_coefficient=Decimal("0.06"),
        poly_maker_coefficient=Decimal("-0.0125"),
        poly_market_coefficients={"special-market": Decimal("0.10")},
    )
    # override: 0.10 * 100 * 0.25 = $2.50
    assert poly_fee_micros(100, 500_000, params=params, market="special-market") == 2_500_000
    assert poly_fee_micros(100, 500_000, params=params, market="other") == 1_500_000


def test_reference_delta_zero_when_unchanged():
    same = FeeParams(**{f: getattr(BASE, f) for f in (
        "kalshi_taker_multiplier", "kalshi_maker_multiplier", "kalshi_precision_micros",
        "poly_taker_coefficient", "poly_maker_coefficient",
    )})
    assert reference_fee_delta_micros(BASE, same) == 0


def test_reference_delta_detects_new_override():
    new = FeeParams(
        kalshi_taker_multiplier=BASE.kalshi_taker_multiplier,
        kalshi_maker_multiplier=BASE.kalshi_maker_multiplier,
        kalshi_precision_micros=BASE.kalshi_precision_micros,
        poly_taker_coefficient=BASE.poly_taker_coefficient,
        poly_maker_coefficient=BASE.poly_maker_coefficient,
        poly_market_coefficients={"m": Decimal("0.12")},  # doubled for one market
    )
    # reference: 0.12 vs 0.06 on 100 @ 0.50 -> $3.00 vs $1.50 -> delta $1.50
    assert reference_fee_delta_micros(BASE, new) == 1_500_000
