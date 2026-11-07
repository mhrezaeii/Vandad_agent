"""Authenticated encryption for credential payloads (AES-256-GCM).

``MasterKeyBox`` is the only component allowed to encrypt/decrypt credential
material. Master keys arrive exclusively through environment configuration
(``CREDENTIAL_MASTER_KEYS`` / ``CREDENTIAL_ACTIVE_KEY_VERSION``); there are no
hardcoded keys and no fallback behavior: a missing, malformed, or too-short
key raises immediately.

Ciphertext layout: ``nonce (12 bytes) || ciphertext || tag`` — produced by the
maintained ``cryptography`` library's AESGCM construction.
"""

from __future__ import annotations

import base64
import binascii
import json
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

AESGCM_KEY_SIZE_BYTES = 32  # AES-256
NONCE_SIZE_BYTES = 12  # GCM standard nonce


class CredentialCryptoError(Exception):
    """Base for credential encryption problems (never carries secret material)."""


class MasterKeyConfigurationError(CredentialCryptoError):
    """Raised when master keys are missing, malformed, or of the wrong size."""


class CredentialAuthenticationError(CredentialCryptoError):
    """Raised when ciphertext fails authentication (tampered or wrong key)."""


class MasterKeyBox:
    """Holds versioned master keys; encrypts with the active version and
    decrypts any loaded version. Fails closed."""

    def __init__(self, keys: dict[int, bytes], active_key_version: int) -> None:
        if not keys:
            raise MasterKeyConfigurationError("no master keys configured")
        for version, key in keys.items():
            if len(key) != AESGCM_KEY_SIZE_BYTES:
                raise MasterKeyConfigurationError(
                    f"master key version {version} must be exactly {AESGCM_KEY_SIZE_BYTES} bytes"
                )
        if active_key_version not in keys:
            raise MasterKeyConfigurationError(
                f"active key version {active_key_version} is not present in the configured master keys"
            )
        self._keys = dict(keys)
        self._active_key_version = active_key_version

    @classmethod
    def from_settings(cls, settings) -> MasterKeyBox:  # noqa: ANN001 - Settings import would be circular
        """Build the box from settings; raises on any configuration problem."""
        try:
            raw: dict[str, str] = json.loads(settings.credential_master_keys)
        except json.JSONDecodeError as exc:
            raise MasterKeyConfigurationError("credential_master_keys is not valid JSON") from exc

        if not raw:
            raise MasterKeyConfigurationError(
                "credential_master_keys is empty; generate one with: openssl rand -base64 32"
            )

        keys: dict[int, bytes] = {}
        for version_text, encoded in raw.items():
            try:
                version = int(version_text)
            except ValueError as exc:
                raise MasterKeyConfigurationError(
                    f"master key version label {version_text!r} must be an integer"
                ) from exc
            try:
                key = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise MasterKeyConfigurationError(
                    f"master key version {version} is not valid base64"
                ) from exc
            keys[version] = key

        return cls(keys=keys, active_key_version=settings.credential_active_key_version)

    @property
    def active_key_version(self) -> int:
        return self._active_key_version

    @property
    def loaded_key_versions(self) -> list[int]:
        return sorted(self._keys)

    def encrypt(self, plaintext: bytes) -> tuple[int, bytes]:
        """Encrypt plaintext with the active key; returns (key_version, ciphertext).

        Ciphertext layout: nonce || ciphertext || tag.
        """
        if not isinstance(plaintext, (bytes, bytearray)):
            raise CredentialCryptoError("plaintext must be bytes")
        nonce = os.urandom(NONCE_SIZE_BYTES)
        ciphertext = AESGCM(self._keys[self._active_key_version]).encrypt(nonce, bytes(plaintext), None)
        return self._active_key_version, nonce + ciphertext

    def decrypt(self, key_version: int, ciphertext: bytes) -> bytes:
        """Decrypt ciphertext produced by a known key version; fails closed."""
        key = self._keys.get(key_version)
        if key is None:
            raise MasterKeyConfigurationError(
                f"master key version {key_version} is not loaded; the credential cannot be decrypted"
            )
        if len(ciphertext) <= NONCE_SIZE_BYTES:
            raise CredentialAuthenticationError("ciphertext is too short to be valid")
        nonce, payload = ciphertext[:NONCE_SIZE_BYTES], ciphertext[NONCE_SIZE_BYTES:]
        try:
            return AESGCM(key).decrypt(nonce, payload, None)
        except InvalidTag as exc:
            raise CredentialAuthenticationError(
                "credential ciphertext failed authentication (wrong key or tampered data)"
            ) from exc
