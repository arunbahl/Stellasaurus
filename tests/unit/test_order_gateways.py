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
