"""Request signers for both venues.

Implemented and unit-tested in Phase 1 but only invoked when credentials are
present; keyless Phase 1 uses public REST reads that need no signature.

Kalshi:     RSA-PSS / SHA-256 over ``{timestamp_ms}{METHOD}{path}`` (query stripped),
            base64-encoded. Headers KALSHI-ACCESS-KEY/-SIGNATURE/-TIMESTAMP.
Polymarket: Ed25519 over ``{timestamp_ms}{METHOD}{path}`` (no delimiters),
            base64-encoded. Headers X-PM-Access-Key/-Timestamp/-Signature. The
            issued secret is the libsodium 64-byte key (seed||pubkey); we sign
            with the first 32 bytes.
"""

from __future__ import annotations

import base64
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey


class KalshiSigner:
    def __init__(self, key_id: str, private_key_pem_path: Path) -> None:
        self.key_id = key_id
        pem = private_key_pem_path.read_bytes()
        key = serialization.load_pem_private_key(pem, password=None)
        if not isinstance(key, RSAPrivateKey):
            raise TypeError("Kalshi private key must be RSA")
        self._key = key

    def sign(self, *, timestamp_ms: int, method: str, path: str) -> str:
        # Kalshi signs the path WITHOUT query parameters.
        path_no_query = path.split("?", 1)[0]
        message = f"{timestamp_ms}{method.upper()}{path_no_query}".encode()
        signature = self._key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=hashes.SHA256.digest_size,
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("ascii")

    def headers(self, *, timestamp_ms: int, method: str, path: str) -> dict[str, str]:
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-SIGNATURE": self.sign(
                timestamp_ms=timestamp_ms, method=method, path=path
            ),
            "KALSHI-ACCESS-TIMESTAMP": str(timestamp_ms),
        }


class PolymarketSigner:
    def __init__(self, access_key: str, ed25519_seed_b64: str) -> None:
        self.access_key = access_key
        raw = base64.b64decode(ed25519_seed_b64)
        # Accept either a raw 32-byte seed or the libsodium/NaCl 64-byte secret
        # key (seed[32] || public_key[32]) that Polymarket issues; the signing
        # seed is the first 32 bytes in the latter.
        if len(raw) == 64:
            seed = raw[:32]
        elif len(raw) == 32:
            seed = raw
        else:
            raise ValueError(
                f"Polymarket Ed25519 secret must decode to 32 or 64 bytes (got {len(raw)})"
            )
        self._key = Ed25519PrivateKey.from_private_bytes(seed)

    def sign(self, *, timestamp_ms: int, method: str, path: str) -> str:
        # Message is timestamp + METHOD + path concatenated, no delimiters
        # (verified against the live API: pipe-separated -> 401 Invalid signature;
        # this form authenticates). Query string is not included.
        message = f"{timestamp_ms}{method.upper()}{path}".encode()
        return base64.b64encode(self._key.sign(message)).decode("ascii")

    def headers(self, *, timestamp_ms: int, method: str, path: str) -> dict[str, str]:
        return {
            "X-PM-Access-Key": self.access_key,
            "X-PM-Timestamp": str(timestamp_ms),
            "X-PM-Signature": self.sign(timestamp_ms=timestamp_ms, method=method, path=path),
        }
