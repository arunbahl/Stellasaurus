"""Identifier and fingerprint helpers (stdlib only).

`terms_fingerprint` is a safety-critical primitive: a change in a market's
resolution terms must flip its registry pair to STALE (DESIGN §6.3 / §10). The
fingerprint must therefore be:

  * stable under irrelevant reordering / whitespace, and
  * sensitive to any change in the fields that define the acceptance criteria.

Tuned by the explicit field set passed in, not by hashing whole raw payloads
(which churn on unrelated fields like volume).
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Mapping
from typing import Any

_WS = re.compile(r"\s+")


def normalize_text(value: str) -> str:
    """Canonicalize free text: NFKC, collapse whitespace, casefold, strip."""
    text = unicodedata.normalize("NFKC", value)
    text = _WS.sub(" ", text)
    return text.strip().casefold()


def _canonical(value: Any) -> Any:
    if isinstance(value, str):
        return normalize_text(value)
    if isinstance(value, Mapping):
        return {k: _canonical(value[k]) for k in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_canonical(v) for v in value]
    return value


def terms_fingerprint(fields: Mapping[str, Any]) -> str:
    """Stable SHA-256 over a canonicalized set of resolution-defining fields.

    `fields` should contain ONLY acceptance-criteria-relevant keys (title, rules,
    settlement source, resolution timestamp, ...), never volatile market stats.
    """
    canonical = _canonical(fields)
    payload = _stable_repr(canonical)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _stable_repr(value: Any) -> str:
    if isinstance(value, dict):
        inner = ",".join(f"{k}={_stable_repr(v)}" for k, v in value.items())
        return "{" + inner + "}"
    if isinstance(value, list):
        return "[" + ",".join(_stable_repr(v) for v in value) + "]"
    return repr(value)


def slugify(value: str) -> str:
    """Lowercase, hyphenated identifier fragment."""
    text = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text or "x"
