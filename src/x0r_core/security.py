from __future__ import annotations

import hashlib
import secrets

KEY_PREFIX = "x0r"


def hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def generate_key() -> tuple[str, str, str]:
    """Return (raw_key, prefix, hash). The raw key is shown to the user exactly once."""
    prefix = secrets.token_hex(4)
    secret = secrets.token_urlsafe(32)
    raw = f"{KEY_PREFIX}_{prefix}_{secret}"
    return raw, prefix, hash_key(raw)


def parse_prefix(raw: str) -> str | None:
    parts = raw.split("_", 2)
    if len(parts) != 3 or parts[0] != KEY_PREFIX or not parts[1]:
        return None
    return parts[1]
