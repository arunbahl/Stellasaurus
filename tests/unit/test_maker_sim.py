"""MakerSim economics: the maker-rebate + taker-hedge net-edge computation.

The two numbers path 1 hinges on are derived from ``MakerSim._net``; this pins
its arithmetic so a fee-sign or rounding regression can't silently flip the
verdict. Constructed via ``__new__`` to exercise ``_net`` in isolation (the
poller's book/loop plumbing is not under test here).
"""

from decimal import Decimal

from stellasaurus.background.maker_sim import MakerSim
from stellasaurus.common.money import PAYOUT_MICROS
from stellasaurus.hot_path.fees import (
    FeeParams,
    kalshi_fee_micros,
    poly_fee_micros,
)

PARAMS = FeeParams(
    kalshi_taker_multiplier=Decimal("0.07"),
    kalshi_maker_multiplier=Decimal("0.0175"),
    kalshi_precision_micros=100,
    poly_taker_coefficient=Decimal("0.06"),
    poly_maker_coefficient=Decimal("-0.0125"),
)


def _sim(qty: int) -> MakerSim:
    sim = MakerSim.__new__(MakerSim)
    sim._fees = PARAMS
    sim._qty = qty
    return sim


def test_net_matches_component_fees_and_rebate_sign() -> None:
    """Per-contract: net = $1 − fill − hedge − (poly_maker + kalshi_taker)/qty,
    and the reported rebate is the positive per-contract magnitude of the
    (negative) poly maker fee. Prices are per-contract but the fee helpers return
    the total over qty, so both are amortised — a unit-consistency guard."""
    qty = 20
    sim = _sim(qty)
    fill_px, hedge_px = 500_000, 510_000
    net, rebate = sim._net(fill_px, hedge_px, "KXTEST", "slug")

    poly_maker = poly_fee_micros(qty, fill_px, params=PARAMS, market="slug", is_maker=True)
    kalshi_taker = kalshi_fee_micros(qty, hedge_px, params=PARAMS, series="KXTEST", is_maker=False)

    assert poly_maker <= 0  # maker coefficient is negative -> a rebate
    assert rebate == (-poly_maker) // qty  # positive, per-contract credit
    fee_pc = (poly_maker + kalshi_taker + qty - 1) // qty
    assert net == PAYOUT_MICROS - fill_px - hedge_px - fee_pc


def test_favorable_maker_fill_is_positive() -> None:
    """A below-market maker fill (0.48) hedged at 0.50 locks a small positive net
    once the rebate is credited — the whole premise of path 1."""
    sim = _sim(20)
    net, rebate = sim._net(480_000, 500_000, "KXTEST", "slug")
    assert rebate > 0
    assert net > 0


def test_rebate_vanishes_to_rounding_at_qty_one() -> None:
    """At a single contract the sub-cent (0.3¢) rebate banker's-rounds to zero;
    this is why the sim posts realistic size (documents the qty=20 default)."""
    sim = _sim(1)
    _, rebate = sim._net(500_000, 510_000, "KXTEST", "slug")
    assert rebate == 0
