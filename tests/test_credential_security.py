"""Credential security tests: encryption, key handling, rotation, revocation,
and verification that plaintext never reaches the database, logs, or API."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CredentialStorageType, CredentialVersion
from app.security.crypto import (
    CredentialAuthenticationError,
    MasterKeyBox,
    MasterKeyConfigurationError,
)
from app.security.secret_value import SecretValue
from app.services.credential_service import CredentialService
from tests.conftest import TEST_MASTER_KEY_CONFIG  # noqa: F401  (ensures env is set)

SECRET = "super-secret-api-token-value"
OTHER_SECRET = "rotated-secret-token-value"


@pytest.fixture
def key_box() -> MasterKeyBox:
    return MasterKeyBox.from_settings(_settings())


def _settings():
    import json

    from app.core.config import Settings

    return Settings(
        credential_master_keys=json.dumps({"1": TEST_MASTER_KEY_CONFIG["1"]}),
        credential_active_key_version=1,
        _env_file=None,
    )


# --- encryption primitive -----------------------------------------------------
def test_encrypt_decrypt_roundtrip(key_box):
    key_version, ciphertext = key_box.encrypt(SECRET.encode())
    assert key_version == 1
    assert key_box.decrypt(key_version, ciphertext) == SECRET.encode()


def test_tampered_ciphertext_fails_authentication(key_box):
    _, ciphertext = key_box.encrypt(SECRET.encode())
    tampered = bytearray(ciphertext)
    tampered[-1] ^= 0xFF
    with pytest.raises(CredentialAuthenticationError):
        key_box.decrypt(1, bytes(tampered))


def test_wrong_key_version_fails(key_box):
    _, ciphertext = key_box.encrypt(SECRET.encode())
    box_with_other_key = MasterKeyBox({2: b"k" * 32}, active_key_version=2)
    with pytest.raises(MasterKeyConfigurationError):
        box_with_other_key.decrypt(1, ciphertext)


@pytest.mark.parametrize(
    "keys, active",
    [
        ({}, 1),  # no keys at all
        ({1: b"short"}, 1),  # wrong size
        ({1: b"x" * 32}, 2),  # active version missing
    ],
)
def test_invalid_key_configurations_fail_closed(keys, active):
    with pytest.raises(MasterKeyConfigurationError):
        MasterKeyBox(keys, active_key_version=active)


def test_malformed_base64_key_fails():
    import json

    from app.core.config import Settings

    with pytest.raises(MasterKeyConfigurationError):
        MasterKeyBox.from_settings(
            Settings(credential_master_keys=json.dumps({"1": "not-base64!!!"}), _env_file=None)
        )


def test_missing_key_configuration_fails():
    from app.core.config import Settings

    with pytest.raises(MasterKeyConfigurationError):
        MasterKeyBox.from_settings(Settings(credential_master_keys="{}", _env_file=None))


def test_secret_value_never_leaks_via_repr_or_str():
    secret = SecretValue(SECRET)
    assert SECRET not in repr(secret)
    assert SECRET not in str(secret)
    assert SECRET not in f"formatted: {secret}"
    assert secret != SECRET  # never equal to raw strings


# --- credential service -------------------------------------------------------
async def _create_encrypted_credential(session: AsyncSession, key_box, platform_id):
    service = CredentialService(session, key_box)
    return await service.create_credential(
        platform_id=platform_id,
        credential_type="api_key",
        secret_reference="vault://acme/api-key",
        storage_type=CredentialStorageType.ENCRYPTED,
        value=SecretValue(SECRET),
    )


async def test_create_and_resolve_encrypted_credential(db_session, key_box, company_and_platform):
    company, platform = company_and_platform
    reference = await _create_encrypted_credential(db_session, key_box, platform.id)

    service = CredentialService(db_session, key_box)
    resolved = await service.resolve_credential(reference.id, company_id=company.id)
    assert resolved.payload is not None
    assert resolved.payload.reveal() == SECRET
    assert resolved.version == 1


async def test_no_plaintext_persisted_in_database(db_session, key_box, company_and_platform):
    company, platform = company_and_platform
    reference = await _create_encrypted_credential(db_session, key_box, platform.id)
    await db_session.commit()

    row = (
        await db_session.execute(
            select(CredentialVersion).where(CredentialVersion.credential_reference_id == reference.id)
        )
    ).scalar_one()
    assert isinstance(row.encrypted_payload, bytes)
    assert SECRET.encode() not in row.encrypted_payload
    assert row.encrypted_payload != SECRET.encode()
    assert row.encryption_key_version == 1


async def test_rotation_creates_new_active_version(db_session, key_box, company_and_platform):
    company, platform = company_and_platform
    reference = await _create_encrypted_credential(db_session, key_box, platform.id)
    reference_id = reference.id
    company_id = company.id  # capture before expire_all() expires the instance

    service = CredentialService(db_session, key_box)
    await service.rotate_credential(
        reference.id, company_id=company_id, new_value=SecretValue(OTHER_SECRET)
    )
    await db_session.commit()
    db_session.expire_all()

    versions = (
        await db_session.execute(
            select(CredentialVersion)
            .where(CredentialVersion.credential_reference_id == reference_id)
            .order_by(CredentialVersion.version)
        )
    ).scalars().all()
    assert [v.version for v in versions] == [1, 2]

    resolved = await service.resolve_credential(reference_id, company_id=company_id)
    assert resolved.payload.reveal() == OTHER_SECRET
    assert resolved.version == 2


async def test_revocation_blocks_resolution(db_session, key_box, company_and_platform):
    company, platform = company_and_platform
    reference = await _create_encrypted_credential(db_session, key_box, platform.id)

    service = CredentialService(db_session, key_box)
    await service.revoke_credential(reference.id, company_id=company.id)
    await db_session.commit()

    from app.core.exceptions import ConflictError

    with pytest.raises(ConflictError):
        await service.resolve_credential(reference.id, company_id=company.id)


async def test_cross_company_resolution_forbidden(db_session, key_box, company_and_platform):
    from app.models import Company

    company, platform = company_and_platform
    reference = await _create_encrypted_credential(db_session, key_box, platform.id)

    other = Company(name="Other Co", slug="other-co")
    db_session.add(other)
    await db_session.commit()

    service = CredentialService(db_session, key_box)
    from app.core.exceptions import ForbiddenError

    with pytest.raises(ForbiddenError):
        await service.resolve_credential(reference.id, company_id=other.id)


async def test_reference_storage_type_stores_no_payload(db_session, key_box, company_and_platform):
    company, platform = company_and_platform
    service = CredentialService(db_session, key_box)
    reference = await service.create_credential(
        platform_id=platform.id,
        credential_type="token",
        secret_reference="env:EXTERNAL_TOKEN",
        storage_type=CredentialStorageType.REFERENCE,
    )
    await db_session.commit()

    resolved = await service.resolve_credential(reference.id, company_id=company.id)
    assert resolved.payload is None  # locator-only: nothing to decrypt locally
    assert resolved.secret_reference == "env:EXTERNAL_TOKEN"


async def test_secret_reference_validation_applies(db_session, key_box, company_and_platform):
    _, platform = company_and_platform
    service = CredentialService(db_session, key_box)
    with pytest.raises(ValueError):
        await service.create_credential(
            platform_id=platform.id,
            credential_type="api_key",
            secret_reference="not a valid reference with spaces",
            storage_type=CredentialStorageType.REFERENCE,
        )


async def test_duplicate_locator_rejected(db_session, key_box, company_and_platform):
    _, platform = company_and_platform
    service = CredentialService(db_session, key_box)
    await service.create_credential(
        platform_id=platform.id,
        credential_type="api_key",
        secret_reference="vault://acme/api-key",
        storage_type=CredentialStorageType.REFERENCE,
    )
    from app.core.exceptions import ConflictError

    with pytest.raises(ConflictError):
        await service.create_credential(
            platform_id=platform.id,
            credential_type="api_key",
            secret_reference="vault://acme/api-key",
            storage_type=CredentialStorageType.REFERENCE,
        )


async def test_ciphertext_is_nondeterministic(db_session, key_box):
    """Same plaintext encrypted twice must produce different ciphertext (random nonce)."""
    first = key_box.encrypt(SECRET.encode())
    second = key_box.encrypt(SECRET.encode())
    assert first[1] != second[1]
