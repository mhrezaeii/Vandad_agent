"""Password hashing helpers.

Uses ``hashlib.scrypt`` from the standard library (memory-hard, no extra
dependency) with a per-password random salt. The stored format is::

    scrypt$<n>$<r>$<p>$<base64 salt>$<base64 derived key>

Verification uses a constant-time comparison. Hashing parameters are encoded
in the digest so future cost increases remain backwards compatible.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

_ALGORITHM = "scrypt"
_N = 2**14
_R = 8
_P = 1
_SALT_BYTES = 16
_KEY_BYTES = 32
_MAX_PASSWORD_LENGTH = 1024


def _b64encode(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _b64decode(value: str) -> bytes:
    return base64.b64decode(value.encode("ascii"), validate=True)


def _derive(password: str, salt: bytes, *, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=n,
        r=r,
        p=p,
        dklen=_KEY_BYTES,
        maxmem=64 * 1024 * 1024,
    )


def hash_password(password: str) -> str:
    """Return a salted scrypt digest for *password*."""
    if len(password) > _MAX_PASSWORD_LENGTH:
        raise ValueError("Password is too long.")
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = _derive(password, salt, n=_N, r=_R, p=_P)
    return f"{_ALGORITHM}${_N}${_R}${_P}${_b64encode(salt)}${_b64encode(digest)}"


def verify_password(password: str, stored: str) -> bool:
    """Return ``True`` when *password* matches the stored digest."""
    if len(password) > _MAX_PASSWORD_LENGTH:
        return False
    try:
        algorithm, raw_n, raw_r, raw_p, raw_salt, raw_digest = stored.split("$")
        if algorithm != _ALGORITHM:
            return False
        n, r, p = int(raw_n), int(raw_r), int(raw_p)
        salt = _b64decode(raw_salt)
        expected = _b64decode(raw_digest)
    except (ValueError, TypeError):
        return False
    if n < 2 or r < 1 or p < 1:
        return False
    try:
        candidate = _derive(password, salt, n=n, r=r, p=p)
    except (ValueError, MemoryError):
        return False
    return hmac.compare_digest(candidate, expected)
