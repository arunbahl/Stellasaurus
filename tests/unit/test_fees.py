"""Fee engine vs published venue examples (DESIGN §6.4 / §11)."""

from decimal import Decimal

from stellasaurus.hot_path.fees import FeeParams, kalshi_fee_micros, poly_fee_micros

PARAMS = FeeParams(
    kalshi_taker_multiplier=Decimal("0.07"),
    kalshi_maker_multiplier=Decimal("0.0175"),
    kalshi_precision_micros=10_000,  # $0.01
    poly_taker_coefficient=Decimal("0.06"),
    poly_maker_coefficient=Decimal("-0.0125"),
)


# --- Kalshi quadratic: published worked examples (fee/contract at p) ---

def test_kalshi_published_example_at_50c():
    # 0.07 * 0.50 * 0.50 = $0.0175/contract; 1 contract -> ceil to $0.02
    assert kalshi_fee_micros(1, 500_000, params=PARAMS) == 20_000
    # 10 contracts -> $0.175 -> ceil to $0.18
    assert kalshi_fee_micros(10, 500_000, params=PARAMS) == 180_000
    # 100 contracts -> $1.75 exactly (no rounding needed)
    assert kalshi_fee_micros(100, 500_000, params=PARAMS) == 1_750_000


def test_kalshi_published_example_at_20c():
    # 0.07 * 0.20 * 0.80 = $0.0112/contract; 100 -> $1.12 exact
    assert kalshi_fee_micros(100, 200_000, params=PARAMS) == 1_120_000


def test_kalshi_published_example_at_10c():
    # 0.07 * 0.10 * 0.90 = $0.0063/contract; 100 -> $0.63 exact
    assert kalshi_fee_micros(100, 100_000, params=PARAMS) == 630_000


def test_kalshi_rounds_up_not_half_even():
    # 3 contracts @ 0.50 -> $0.0525 -> must round UP to $0.06 (not to $0.05)
    assert kalshi_fee_micros(3, 500_000, params=PARAMS) == 60_000


def test_kalshi_maker_multiplier_much_lower():
    taker = kalshi_fee_micros(100, 500_000, params=PARAMS)
    maker = kalshi_fee_micros(100, 500_000, params=PARAMS, is_maker=True)
    assert maker == 440_000  # 0.0175*100*0.25 = $0.4375 -> ceil $0.44
    assert maker < taker


def test_kalshi_direct_member_precision():
    fine = FeeParams(
        kalshi_taker_multiplier=Decimal("0.07"),
        kalshi_maker_multiplier=Decimal("0.0175"),
        kalshi_precision_micros=100,  # $0.0001 direct members
        poly_taker_coefficient=Decimal("0.06"), poly_maker_coefficient=Decimal("-0.0125"),
    )
    # $0.0175 needs no rounding at $0.0001 precision
    assert kalshi_fee_micros(1, 500_000, params=fine) == 17_500


def test_kalshi_zero_contracts():
    assert kalshi_fee_micros(0, 500_000, params=PARAMS) == 0


# --- Polymarket: quadratic 0.06 taker / -0.0125 maker rebate, banker's cent ---
# (verified against docs.polymarket.us/fees worked examples, 2026-07-06)

def test_poly_taker_docs_example_max_at_50c():
    # docs: taker max $1.50 per 100 contracts at p=$0.50
    assert poly_fee_micros(100, 500_000, params=PARAMS) == 1_500_000


def test_poly_taker_docs_example_1000_contracts():
    # docs: 1000 @ p=0.10 -> 0.06*1000*0.09 = $5.40 ; p=0.50 -> $15.00
    assert poly_fee_micros(1000, 100_000, params=PARAMS) == 5_400_000
    assert poly_fee_micros(1000, 500_000, params=PARAMS) == 15_000_000


def test_poly_maker_rebate_negative():
    # docs: maker max -$0.31 per 100 at p=0.50 (banker's: -31.25c -> -31c... 
    # -0.3125 dollars -> -31.25 cents -> half-to-even -> -31.25 rounds to -31? 
    # quantize(-31.25) HALF_EVEN -> -31.2 -> int cents = -31
    fee = poly_fee_micros(100, 500_000, params=PARAMS, is_maker=True)
    assert fee == -310_000  # -$0.31 rebate


def test_poly_bankers_rounding_to_cent():
    # 10 @ 0.45: 0.06*10*0.45*0.55 = $0.1485 -> $0.15 (half-even on 14.85c -> 15c)
    assert poly_fee_micros(10, 450_000, params=PARAMS) == 150_000
