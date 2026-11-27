"""Signed caller tokens: carry "who this is for" across a hop that can't vouch for it.

The issuer (Core, or your app) holds a root secret and signs short-lived tokens. Each
token is signed with a key derived for its audience, and a verifier gets only that
derived key. So a proxy can't mint tokens for any other service, Core included:

    proxy_key = audience_key(ROOT, "mcp-proxy")          # give this to the proxy
    token = sign_caller(Caller("user-42", {"orders": ("A-1", "A-2")}),
                        key_id="ck_1", secret=ROOT, audience="mcp-proxy")
    caller = verify_caller(token, keys={"ck_1": proxy_key}, audience="mcp-proxy")

The derived key is still symmetric: whoever holds it could mint tokens for that one
audience, so treat a verifier as trusted for its own audience.

Format: ``x0r.v1.<payload>.<signature>``. The payload is base64url JSON
``{"kid", "sub", "attrs", "aud", "iat", "exp"}`` and the signature is HMAC-SHA256 over
``x0r.v1.<payload>``, keyed with ``audience_key(root, aud)``. Standard library only.

Anything wrong with a token raises ``CallerTokenError``. Its ``reason`` is the denial
reason to record. A bad token never turns into "no caller".
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
import time
from collections.abc import Mapping
from typing import Any

from .engine import Caller

PREFIX = "x0r.v1"
MAX_TOKEN_LENGTH = 8192
MAX_TTL_SECONDS = 24 * 3600
DEFAULT_TTL_SECONDS = 300
CLOCK_LEEWAY_SECONDS = 30
MIN_SECRET_BYTES = 32

MAX_SUBJECT = 200
MAX_ATTRIBUTES = 20
MAX_ATTRIBUTE_NAME = 64
MAX_ATTRIBUTE_VALUE = 200
MAX_LIST_ITEMS = 100


class CallerTokenError(Exception):
    """The caller token can't be trusted. ``reason`` is a stable denial reason code."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64decode(text: str) -> bytes:
    if not text or any(c not in _B64_CHARS for c in text):
        raise ValueError("not base64url")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


_B64_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")


def _clean_text(value: object, limit: int, what: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{what} must be a non-empty string of at most {limit} characters")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ValueError(f"{what} must not contain control characters")
    _utf8(value, what)
    return value


def _utf8(value: str, what: str) -> None:
    # JSON allows lone surrogates like "\ud800"; nothing downstream can store them.
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError(f"{what} must be valid Unicode") from None


def normalize_attributes(attributes: Mapping[str, Any]) -> dict[str, str | tuple[str, ...]]:
    """Check caller attributes and return them as strings or tuples of strings."""
    if not isinstance(attributes, Mapping):
        raise ValueError("attributes must be an object")
    if len(attributes) > MAX_ATTRIBUTES:
        raise ValueError(f"at most {MAX_ATTRIBUTES} attributes")
    clean: dict[str, str | tuple[str, ...]] = {}
    for name, value in attributes.items():
        _clean_text(name, MAX_ATTRIBUTE_NAME, "attribute name")
        if isinstance(value, str):
            if len(value) > MAX_ATTRIBUTE_VALUE:
                raise ValueError(f"attribute {name!r} is too long")
            if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
                raise ValueError(f"attribute {name!r} must not contain control characters")
            _utf8(value, f"attribute {name!r}")
            clean[name] = value
        elif isinstance(value, (list, tuple)):
            if len(value) > MAX_LIST_ITEMS:
                raise ValueError(f"attribute {name!r} has more than {MAX_LIST_ITEMS} items")
            clean[name] = tuple(
                _clean_text(item, MAX_ATTRIBUTE_VALUE, f"an item of {name!r}") for item in value
            )
        else:
            raise ValueError(f"attribute {name!r} must be a string or a list of strings")
    return clean


def _signature(secret: bytes, signed_part: str) -> bytes:
    return hmac.new(secret, signed_part.encode("ascii"), hashlib.sha256).digest()


def audience_key(secret: bytes, audience: str) -> bytes:
    """The key that signs and checks tokens for one audience. Give verifiers this."""
    root = _check_secret(secret)
    label = "x0r-caller-v1|" + _clean_text(audience, 200, "audience")
    return hmac.new(root, label.encode("utf-8"), hashlib.sha256).digest()


def _check_secret(secret: object) -> bytes:
    if not isinstance(secret, (bytes, bytearray)) or len(secret) < MIN_SECRET_BYTES:
        raise ValueError(f"a caller key secret must be at least {MIN_SECRET_BYTES} bytes")
    return bytes(secret)


def sign_caller(
    caller: Caller,
    *,
    key_id: str,
    secret: bytes,
    audience: str,
    ttl: int = DEFAULT_TTL_SECONDS,
    now: float | None = None,
) -> str:
    """Return a token for ``caller`` that ``verify_caller`` accepts until it expires."""
    if not isinstance(caller, Caller):
        raise TypeError("sign_caller() needs a Caller")
    if type(ttl) is not int or not 1 <= ttl <= MAX_TTL_SECONDS:
        raise ValueError(f"ttl must be 1 to {MAX_TTL_SECONDS} seconds")
    issued = int(time.time() if now is None else now)
    attrs = normalize_attributes(caller.attributes)
    payload = {
        "kid": _key_id(key_id),
        "sub": _clean_text(caller.subject, MAX_SUBJECT, "subject"),
        "attrs": {k: list(v) if isinstance(v, tuple) else v for k, v in attrs.items()},
        "aud": _clean_text(audience, 200, "audience"),
        "iat": issued,
        "exp": issued + ttl,
    }
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    signed_part = f"{PREFIX}.{_b64encode(body.encode('ascii'))}"
    key = audience_key(secret, audience)
    token = f"{signed_part}.{_b64encode(_signature(key, signed_part))}"
    if len(token) > MAX_TOKEN_LENGTH:
        raise ValueError("the caller is too large to fit in a token")
    return token


def _key_id(key_id: object) -> str:
    if not isinstance(key_id, str) or not _KEY_ID.fullmatch(key_id):
        raise ValueError("key_id must be 1 to 64 letters, digits, '_' or '-'")
    return key_id


def _malformed(message: str) -> CallerTokenError:
    return CallerTokenError("caller_token_malformed", message)


def _payload(token: object) -> tuple[str, bytes, dict[str, Any]]:
    if not isinstance(token, str) or len(token) > MAX_TOKEN_LENGTH:
        raise _malformed("caller token must be a string of at most 8192 characters")
    parts = token.split(".")
    if len(parts) != 4 or f"{parts[0]}.{parts[1]}" != PREFIX:
        raise _malformed("not an x0r.v1 caller token")
    try:
        body = _b64decode(parts[2])
        signature = _b64decode(parts[3])
        payload = json.loads(body.decode("utf-8"), parse_constant=_reject_constant)
    except (ValueError, UnicodeDecodeError, binascii.Error, RecursionError) as exc:
        raise _malformed("caller token can't be decoded") from exc
    if not isinstance(payload, dict):
        raise _malformed("caller token payload must be an object")
    return f"{PREFIX}.{parts[2]}", signature, payload


def _reject_constant(name: str) -> None:
    raise ValueError(f"{name} is not allowed")


def token_key_id(token: object) -> str:
    """The key id a token claims, read before checking it, to look up the right key.

    Nothing in the token is trusted until ``verify_caller`` accepts it.
    """
    _, _, payload = _payload(token)
    kid = payload.get("kid")
    if not isinstance(kid, str) or not _KEY_ID.fullmatch(kid):
        raise _malformed("caller token has no valid key id")
    return kid


_KEY_ID = re.compile(r"[A-Za-z0-9_\-]{1,64}")


def verify_caller(
    token: str,
    *,
    keys: Mapping[str, bytes],
    audience: str,
    now: float | None = None,
) -> Caller:
    """Check ``token`` and return the caller it names, or raise ``CallerTokenError``.

    ``keys`` maps key ids to audience keys (``audience_key(root, audience)``). Pass every
    active key, so tokens signed with the previous key keep working while you rotate.
    """
    signed_part, signature, payload = _payload(token)
    kid = payload.get("kid")
    secret = keys.get(kid) if isinstance(kid, str) else None
    # An unknown key id is still compared against a dummy key, so the code path is alike.
    expected = _signature(_check_secret(secret) if secret is not None else b"\0" * 32,
                          signed_part)
    if secret is None or not hmac.compare_digest(expected, signature):
        raise CallerTokenError("caller_signature_invalid", "caller token signature is invalid")

    # The signature is good, so the payload is what the signer wrote. Check it anyway:
    # a buggy signer must not produce a caller the rest of the guard can't handle.
    if set(payload) != {"kid", "sub", "attrs", "aud", "iat", "exp"}:
        raise _malformed("caller token has missing or extra fields")
    iat, exp = payload["iat"], payload["exp"]
    if type(iat) is not int or type(exp) is not int or exp <= iat:
        raise _malformed("caller token times are invalid")
    if exp - iat > MAX_TTL_SECONDS:
        raise _malformed("caller token lives longer than 24 hours")
    if payload["aud"] != audience:
        raise CallerTokenError("caller_wrong_audience", "caller token is for another service")
    current = time.time() if now is None else now
    if iat > current + CLOCK_LEEWAY_SECONDS:
        raise _malformed("caller token was issued in the future")
    if current >= exp + CLOCK_LEEWAY_SECONDS:
        raise CallerTokenError("caller_expired", "caller token has expired")
    try:
        subject = _clean_text(payload["sub"], MAX_SUBJECT, "subject")
        attributes = normalize_attributes(payload["attrs"])
    except ValueError as exc:
        raise _malformed(str(exc)) from exc
    return Caller(subject, attributes)
