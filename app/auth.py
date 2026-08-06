"""Optional Bearer API-key authentication for Forage.

Keys come from the FORAGE_API_KEYS env var (comma-separated). Comparison is
constant-time (hmac.compare_digest) to avoid timing attacks. When
auth.enabled is false (default), every request passes.
"""

from __future__ import annotations

import hmac
import os
from typing import List, Optional


def load_api_keys() -> List[str]:
    """Load API keys from FORAGE_API_KEYS (comma-separated)."""
    raw = os.environ.get("FORAGE_API_KEYS", "")
    return [k.strip() for k in raw.split(",") if k.strip()]


def extract_bearer(authorization: Optional[str]) -> Optional[str]:
    """Pull the token out of an Authorization: Bearer <key> header."""
    if not authorization:
        return None
    scheme, _, rest = authorization.partition(" ")
    if scheme.lower() != "bearer" or not rest:
        return None
    return rest.strip()


def key_is_valid(provided: Optional[str], keys: List[str]) -> bool:
    """Constant-time check of a provided key against the configured keys."""
    if not provided or not keys:
        return False
    for key in keys:
        if hmac.compare_digest(provided.encode("utf-8"), key.encode("utf-8")):
            return True
    return False
