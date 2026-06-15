"""Signer byte-correctness against the crypto primitives (verify round-trips)."""

import base64

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.rsa import generate_private_key

from stellasaurus.venues.signing import KalshiSigner, PolymarketSigner


def test_polymarket_ed25519_signature_verifies():
    key = Ed25519PrivateKey.generate()
    seed = key.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    signer = PolymarketSigner("akey", base64.b64encode(seed).decode())
    ts, method, path = 1_700_000_000_000, "GET", "/v1/markets"
    sig = signer.sign(timestamp_ms=ts, method=method, path=path)
    message = f"{ts}|{method}|{path}".encode()
    key.public_key().verify(base64.b64decode(sig), message)  # raises if invalid

    headers = signer.headers(timestamp_ms=ts, method="get", path=path)
    assert headers["X-PM-Access-Key"] == "akey"
    assert headers["X-PM-Timestamp"] == str(ts)


def test_kalshi_rsa_pss_signature_verifies_and_strips_query(tmp_path):
    key = generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    key_path = tmp_path / "k.pem"
    key_path.write_bytes(pem)
    signer = KalshiSigner("kid", key_path)

    ts = 1_700_000_000_000
    # query must be stripped before signing
    sig = signer.sign(timestamp_ms=ts, method="GET", path="/portfolio/orders?limit=5")
    message = f"{ts}GET/portfolio/orders".encode()
    key.public_key().verify(
        base64.b64decode(sig),
        message,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=hashes.SHA256.digest_size),
        hashes.SHA256(),
    )
