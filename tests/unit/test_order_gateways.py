"""Order gateways: hard gate + request-shape checks (no network)."""

import pytest

from stellasaurus.common.config import Settings
from stellasaurus.common.types import OutcomePolarity, Side
from stellasaurus.venues.orders import (
    KalshiOrderGateway,
    LiveGateDisabledError,
    PolymarketOrderGateway,
)


class _NoHttp:
    """Fails the test if any request is attempted."""

    async def post(self, *a, **kw):  # noqa: ANN002, ANN003
        raise AssertionError("network must not be touched")


def _settings(**kw) -> Settings:
    base = dict(
        poly_access_key="k", poly_ed25519_seed="A" * 43 + "=",  # invalid, unused here
        live_trading_enabled=False,
    )
    base.update(kw)
    return Settings(**base)


def test_poly_gateway_requires_credentials():
    with pytest.raises(ValueError):
        PolymarketOrderGateway(Settings(poly_access_key=None, poly_ed25519_seed=None), _NoHttp())


def test_kalshi_gateway_requires_credentials():
    with pytest.raises(ValueError):
        KalshiOrderGateway(Settings(kalshi_api_key_id=None), _NoHttp())


async def test_poly_gateway_refuses_when_gate_off(tmp_path):
    # real-looking Ed25519 seed so the signer constructs
    import base64

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    seed = Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    s = Settings(
        poly_access_key="key", poly_ed25519_seed=base64.b64encode(seed).decode(),
        live_trading_enabled=False,
    )
    gw = PolymarketOrderGateway(s, _NoHttp())
    with pytest.raises(LiveGateDisabledError):
        await gw.buy_fok(native_id="slug", side=Side.YES, qty=1,
                         limit_price_micros=500_000, polarity=OutcomePolarity.DIRECT)


async def test_kalshi_gateway_refuses_when_gate_off(tmp_path):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.rsa import generate_private_key
    pem = generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    key_path = tmp_path / "k.pem"
    key_path.write_bytes(pem)
    s = Settings(
        kalshi_api_key_id="kid", kalshi_private_key_path=key_path,
        live_trading_enabled=False,
    )
    gw = KalshiOrderGateway(s, _NoHttp())
    with pytest.raises(LiveGateDisabledError):
        await gw.buy_fok(native_id="KX", side=Side.NO, qty=1,
                         limit_price_micros=500_000, polarity=OutcomePolarity.DIRECT)


def test_cent_tick_rounding_directions():
    from stellasaurus.venues.orders import _ceil_cent, _floor_cent
    # buys floor (never exceed intent); 0.6153 -> 0.61
    assert _floor_cent(615_300) == 610_000
    # asks ceil (never sell YES cheaper than intent); 0.3847 -> 0.39
    assert _ceil_cent(384_700) == 390_000
    # on-tick values unchanged
    assert _floor_cent(610_000) == 610_000 and _ceil_cent(390_000) == 390_000
    # buy limit never floors to zero
    assert _floor_cent(900) == 10_000


def _poly_gateway_live():
    """A live-enabled Poly gateway whose signer/http we can drive."""
    import base64

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    seed = Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    s = Settings(
        poly_access_key="key", poly_ed25519_seed=base64.b64encode(seed).decode(),
        live_trading_enabled=True,
    )

    class _Resp:
        def __init__(self, payload):
            self._p = payload
            self.status_code = 200

        def json(self):
            return self._p

        def raise_for_status(self):
            return None

    class _FakeHttp:
        """POST returns empty executions (the live footgun); GET returns the
        authoritative filled order."""

        def __init__(self, order_payload):
            self.order_payload = order_payload

        async def post(self, *a, **kw):  # noqa: ANN002, ANN003
            return _Resp({"id": "OID1", "executions": []})

        async def get(self, *a, **kw):  # noqa: ANN002, ANN003
            return _Resp({"order": self.order_payload})

    return s, _FakeHttp


async def test_poly_fill_detected_via_cumquantity_not_executions():
    """Regression: create response has empty executions even on a full fill;
    the gateway MUST read cumQuantity from the order lookup."""
    s, FakeHttp = _poly_gateway_live()
    http = FakeHttp({"cumQuantity": 1, "leavesQuantity": 0,
                     "price": {"value": "0.57"}})
    gw = PolymarketOrderGateway(s, http)
    res = await gw.buy_fok(native_id="slug", side=Side.YES, qty=1,
                           limit_price_micros=590_000, polarity=OutcomePolarity.DIRECT)
    assert res.filled_qty == 1  # NOT 0 — the bug reported this as a miss
    assert res.fully_filled
    assert res.avg_price_micros == 570_000


async def test_poly_killed_fok_reports_zero_fill():
    """A killed FOK: empty executions AND cumQuantity 0 -> genuine miss."""
    s, FakeHttp = _poly_gateway_live()
    http = FakeHttp({"cumQuantity": 0, "leavesQuantity": 0})
    gw = PolymarketOrderGateway(s, http)
    res = await gw.buy_fok(native_id="slug", side=Side.NO, qty=1,
                           limit_price_micros=410_000, polarity=OutcomePolarity.DIRECT)
    assert res.filled_qty == 0
    assert not res.fully_filled
