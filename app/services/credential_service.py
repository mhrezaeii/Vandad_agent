"""CredentialService: the trusted credential boundary.

This is the ONLY component that handles plaintext credential material:

- creates credential references (external locator or locally encrypted payload)
- rotates (new version) and revokes credentials
- resolves the active credential for a connection, enforcing tenant ownership

Everything it returns to callers carries secret material exclusively as
``SecretValue``; resolution results are intended for the provider execution
context and nothing else. No method logs secret values.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, ForbiddenError, NotFoundError
from app.models import (
    CredentialReference,
    CredentialStatus,
    CredentialStorageType,
    CredentialVersion,
    CredentialVersionStatus,
    Platform,
)
from app.security.crypto import MasterKeyBox
from app.security.redaction import validate_secret_reference
from app.security.secret_value import SecretValue
from app.services.audit_service import AuditService

_CREDENTIAL_RESOURCE = "credential"


@dataclass(frozen=True)
class ResolvedCredential:
    """Plaintext credential material for provider execution.

    ``payload`` is a ``SecretValue``: it never renders in logs, reprs, or
    error messages. ``metadata`` contains only non-secret facts.
    """

    credential_reference_id: uuid.UUID
    credential_type: str
    storage_type: str
    version: int | None
    payload: SecretValue | None  # None for locator-only credentials
    secret_reference: str


class CredentialService:
    def __init__(self, session: AsyncSession, master_key_box: MasterKeyBox) -> None:
        self.session = session
        self._keys = master_key_box
        self._audit = AuditService(session)

    # --- creation ------------------------------------------------------------
    async def create_credential(
        self,
        *,
        platform_id: uuid.UUID,
        credential_type: CredentialType,
        secret_reference: str,
        storage_type: CredentialStorageType = CredentialStorageType.REFERENCE,
        value: SecretValue | None = None,
    ) -> CredentialReference:
        """Create a credential reference; optionally store an encrypted payload v1.

        ``value`` must be provided (and is required to be a SecretValue) when
        ``storage_type`` is ENCRYPTED.
        """
        validate_secret_reference(secret_reference)
        company_id = await self._require_platform_company(platform_id)

        if await self._find_by_locator(platform_id, secret_reference) is not None:
            raise ConflictError(
                "A credential with this secret_reference already exists for the platform.",
                details=[{"field": "secret_reference", "value": secret_reference}],
            )

        reference = CredentialReference(
            platform_id=platform_id,
            credential_type=credential_type.value,
            secret_reference=secret_reference,
            status=CredentialStatus.ACTIVE.value,
            storage_type=storage_type.value,
        )
        self.session.add(reference)
        await self.session.flush()

        if storage_type is CredentialStorageType.ENCRYPTED:
            if value is None:
                raise ValueError("encrypted credentials require a value")
            key_version, ciphertext = self._keys.encrypt(value.reveal().encode("utf-8"))
            self.session.add(
                CredentialVersion(
                    credential_reference_id=reference.id,
                    version=1,
                    encrypted_payload=ciphertext,
                    encryption_key_version=key_version,
                    status=CredentialVersionStatus.ACTIVE.value,
                )
            )
            await self.session.flush()
            reference.current_version_id = await self._latest_version_id(reference.id)

        self._audit.record(
            event_type="credential.created",
            action="create",
            company_id=company_id,
            platform_id=platform_id,
            resource_type=_CREDENTIAL_RESOURCE,
            resource_id=reference.id,
            metadata={
                "credential_type": reference.credential_type,
                "storage_type": reference.storage_type,
                "secret_reference": reference.secret_reference,
            },
        )
        return reference

    # --- rotation / revocation -------------------------------------------------
    async def rotate_credential(
        self,
        reference_id: uuid.UUID,
        *,
        company_id: uuid.UUID,
        new_value: SecretValue | None = None,
        new_secret_reference: str | None = None,
    ) -> CredentialReference:
        """Create a new active version (encrypted) or update the locator."""
        reference = await self.get_credential(reference_id, company_id=company_id)
        next_version = (await self._latest_version_number(reference.id)) + 1

        if new_secret_reference is not None:
            validate_secret_reference(new_secret_reference)
            reference.secret_reference = new_secret_reference

        if reference.storage_type == CredentialStorageType.ENCRYPTED.value:
            if new_value is None:
                raise ValueError("rotating an encrypted credential requires a new value")
            key_version, ciphertext = self._keys.encrypt(new_value.reveal().encode("utf-8"))
            version = CredentialVersion(
                credential_reference_id=reference.id,
                version=next_version,
                encrypted_payload=ciphertext,
                encryption_key_version=key_version,
                status=CredentialVersionStatus.ACTIVE.value,
            )
            self.session.add(version)
            await self.session.flush()
            reference.current_version_id = version.id

        self._audit.record(
            event_type="credential.rotated",
            action="rotate",
            company_id=company_id,
            platform_id=reference.platform_id,
            resource_type=_CREDENTIAL_RESOURCE,
            resource_id=reference.id,
            metadata={"version": next_version},
        )
        return reference

    async def revoke_credential(self, reference_id: uuid.UUID, *, company_id: uuid.UUID) -> CredentialReference:
        """Revoke the active version (encrypted) or the whole reference."""
        from datetime import UTC, datetime

        reference = await self.get_credential(reference_id, company_id=company_id)
        if reference.status == CredentialStatus.REVOKED.value:
            return reference

        if reference.storage_type == CredentialStorageType.ENCRYPTED.value and reference.current_version_id:
            current = await self.session.get(CredentialVersion, reference.current_version_id)
            if current is not None:
                current.status = CredentialVersionStatus.REVOKED.value
                current.revoked_at = datetime.now(UTC)
        reference.status = CredentialStatus.REVOKED.value

        self._audit.record(
            event_type="credential.revoked",
            action="revoke",
            company_id=company_id,
            platform_id=reference.platform_id,
            resource_type=_CREDENTIAL_RESOURCE,
            resource_id=reference.id,
            metadata={"secret_reference": reference.secret_reference},
        )
        return reference

    # --- resolution (trusted boundary) -----------------------------------------
    async def resolve_credential(
        self, reference_id: uuid.UUID, *, company_id: uuid.UUID
    ) -> ResolvedCredential:
        """Resolve the active credential payload; enforces tenant ownership.

        Returns ``payload=None`` for locator-only credentials; providers that
        require a local secret treat that as a configuration error.
        """
        reference = await self.get_credential(reference_id, company_id=company_id)

        if reference.status != CredentialStatus.ACTIVE.value:
            raise ConflictError(
                "The credential is not active.",
                details=[{"field": "credential_id", "value": str(reference_id)}],
            )

        if reference.storage_type == CredentialStorageType.REFERENCE.value:
            return ResolvedCredential(
                credential_reference_id=reference.id,
                credential_type=reference.credential_type,
                storage_type=reference.storage_type,
                version=None,
                payload=None,
                secret_reference=reference.secret_reference,
            )

        if reference.current_version_id is None:
            raise ConflictError(
                "The credential has no active version.",
                details=[{"field": "credential_id", "value": str(reference_id)}],
            )
        version = await self.session.get(CredentialVersion, reference.current_version_id)
        if version is None or version.status != CredentialVersionStatus.ACTIVE.value:
            raise ConflictError(
                "The credential has no active version.",
                details=[{"field": "credential_id", "value": str(reference_id)}],
            )
        if version.encrypted_payload is None or version.encryption_key_version is None:
            raise ConflictError(
                "The credential version has no encrypted payload.",
                details=[{"field": "credential_id", "value": str(reference_id)}],
            )

        plaintext = self._keys.decrypt(version.encryption_key_version, version.encrypted_payload)
        return ResolvedCredential(
            credential_reference_id=reference.id,
            credential_type=reference.credential_type,
            storage_type=reference.storage_type,
            version=version.version,
            payload=SecretValue(plaintext.decode("utf-8")),
            secret_reference=reference.secret_reference,
        )

    # --- lookups ---------------------------------------------------------------
    async def get_credential(self, reference_id: uuid.UUID, *, company_id: uuid.UUID) -> CredentialReference:
        reference = await self.session.get(CredentialReference, reference_id)
        if reference is None:
            raise NotFoundError(
                "Credential not found.", details=[{"field": "credential_id", "value": str(reference_id)}]
            )
        platform = await self.session.get(Platform, reference.platform_id)
        if platform is None or platform.company_id != company_id:
            # Cross-tenant access attempt: do not reveal existence.
            raise ForbiddenError("You are not allowed to access this credential.")
        return reference

    async def _find_by_locator(self, platform_id: uuid.UUID, secret_reference: str) -> CredentialReference | None:
        result = await self.session.execute(
            select(CredentialReference).where(
                CredentialReference.platform_id == platform_id,
                CredentialReference.secret_reference == secret_reference,
            )
        )
        return result.scalar_one_or_none()

    async def _latest_version_number(self, reference_id: uuid.UUID) -> int:
        latest_id = await self._latest_version_id(reference_id)
        if latest_id is None:
            return 0
        version = await self.session.get(CredentialVersion, latest_id)
        return version.version if version else 0

    async def _latest_version_id(self, reference_id: uuid.UUID) -> uuid.UUID | None:
        result = await self.session.execute(
            select(CredentialVersion.id)
            .where(CredentialVersion.credential_reference_id == reference_id)
            .order_by(CredentialVersion.version.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def _require_platform_company(self, platform_id: uuid.UUID) -> uuid.UUID:
        platform = await self.session.get(Platform, platform_id)
        if platform is None:
            raise NotFoundError(
                "Platform not found.", details=[{"field": "platform_id", "value": str(platform_id)}]
            )
        return platform.company_id
