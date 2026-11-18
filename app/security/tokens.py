"""Signed bearer access tokens for application users.

Tokens are compact, self-contained and HMAC-SHA256 signed::

    base64url(payload_json) + "." + base64url(signature)

They intentionally carry only the user id, issue/expiry timestamps and the
issuer. Approval state, role and company are always re-read from the database
on every request, so revoking or approving an account takes effect immediately
instead of waiting for the token to expire.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from app.core.config import Settings
from app.core.exceptions import UnauthorizedError

_ISSUER = "vandad-agent"
_DEV_SECRET_SEED = b"vandad-agent-local-development-token-key"


class AuthTokenError(UnauthorizedError):
    """Raised when a bearer token is missing, malformed, forged or expired."""

    default_message = "The access token is invalid or has expired."


@dataclass(frozen=True)
class TokenClaims:
    subject: uuid.UUID
    issued_at: datetime
    expires_at: datetime


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def token_signing_key(settings: Settings) -> bytes:
    """Resolve the HMAC key, refusing insecure defaults in staging/production."""
    configured = settings.auth_token_secret
    if configured is not None and configured.get_secret_value():
        return configured.get_secret_value().encode("utf-8")
    if settings.requires_proxy_auth:
        raise AuthTokenError("Server token signing is not configured.")
    return hashlib.sha256(_DEV_SECRET_SEED).digest()


def create_access_token(
    subject: uuid.UUID | str,
    settings: Settings,
    *,
    ttl_seconds: int | None = None,
) -> tuple[str, datetime]:
    """Create a signed token for *subject*; returns ``(token, expires_at)``."""
    now = int(time.time())
    lifetime = ttl_seconds if ttl_seconds is not None else settings.auth_token_ttl_seconds
    payload = {
        "sub": str(subject),
        "iss": _ISSUER,
        "iat": now,
        "exp": now + int(lifetime),
    }
    encoded_payload = _b64url_encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    signature = hmac.new(
        token_signing_key(settings), encoded_payload.encode("ascii"), hashlib.sha256
    ).digest()
    token = f"{encoded_payload}.{_b64url_encode(signature)}"
    return token, datetime.fromtimestamp(payload["exp"], tz=UTC)


def decode_access_token(token: str, settings: Settings) -> TokenClaims:
    """Verify signature and expiry, returning the token claims."""
    if not token or token.count(".") != 1:
        raise AuthTokenError()
    encoded_payload, encoded_signature = token.split(".", 1)
    try:
        supplied_signature = _b64url_decode(encoded_signature)
    except (ValueError, TypeError) as exc:
        raise AuthTokenError() from exc
    expected_signature = hmac.new(
        token_signing_key(settings), encoded_payload.encode("ascii"), hashlib.sha256
    ).digest()
    if not hmac.compare_digest(supplied_signature, expected_signature):
        raise AuthTokenError()

    try:
        payload = json.loads(_b64url_decode(encoded_payload))
        if payload.get("iss") != _ISSUER:
            raise AuthTokenError()
        subject = uuid.UUID(str(payload["sub"]))
        issued_at = int(payload["iat"])
        expires_at = int(payload["exp"])
    except AuthTokenError:
        raise
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise AuthTokenError() from exc

    if expires_at <= int(time.time()):
        raise AuthTokenError("The access token has expired.")
    if expires_at <= issued_at:
        raise AuthTokenError()

    return TokenClaims(
        subject=subject,
        issued_at=datetime.fromtimestamp(issued_at, tz=UTC),
        expires_at=datetime.fromtimestamp(expires_at, tz=UTC),
    )


def bearer_token_from_header(header_value: str | None) -> str | None:
    """Extract the token from an ``Authorization`` header, if present."""
    if not header_value:
        return None
    scheme, _, credentials = header_value.partition(" ")
    if scheme.lower() != "bearer":
        return None
    return credentials.strip() or None
