"""CredentialReference model: credential metadata and payload routing.

Two storage kinds exist:

- ``reference``: the secret lives in an external store; ``secret_reference``
  holds a locator (e.g. ``vault://platform-x/api-key``) validated by
  ``app.security.redaction``. No secret material is ever stored locally.
- ``encrypted``: the secret value was provided once (over TLS) and is stored
  only as AES-256-GCM ciphertext inside ``CredentialVersion`` rows. The
  plaintext never touches the database, logs, or API responses.

Credential values are versioned (``CredentialVersion``); ``current_version_id``
points at the active version used for resolution and rotation.
"""

from __future__ import annotations

import enum
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, ForeignKey, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.credential_version import CredentialVersion
    from app.models.platform import Platform


class CredentialType(enum.StrEnum):
    API_KEY = "api_key"
    OAUTH2 = "oauth2"
    BASIC_AUTH = "basic_auth"
    BROWSER_SESSION = "browser_session"
    TOKEN = "token"


class CredentialStatus(enum.StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"
    EXPIRED = "expired"


class CredentialStorageType(enum.StrEnum):
    REFERENCE = "reference"
    ENCRYPTED = "encrypted"


class CredentialReference(Base, TimestampMixin):
    __tablename__ = "credential_references"
    __table_args__ = (
        UniqueConstraint("platform_id", "secret_reference", name="uq_credential_references_platform_reference"),
        CheckConstraint(
            "credential_type IN ('api_key', 'oauth2', 'basic_auth', 'browser_session', 'token')",
            name="credential_type_valid",
        ),
        CheckConstraint("status IN ('active', 'revoked', 'expired')", name="status_valid"),
        CheckConstraint("storage_type IN ('reference', 'encrypted')", name="storage_type_valid"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # RESTRICT: a platform with tracked credentials cannot be deleted until the
    # credential references are removed explicitly.
    platform_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("platforms.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    credential_type: Mapped[str] = mapped_column(String(32), nullable=False)
    secret_reference: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=CredentialStatus.ACTIVE.value, server_default=CredentialStatus.ACTIVE.value
    )
    # How the payload is stored: external locator only, or encrypted locally.
    storage_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=CredentialStorageType.REFERENCE.value,
        server_default=CredentialStorageType.REFERENCE.value,
    )
    # Active encrypted payload version (NULL for reference-type credentials).
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("credential_versions.id", ondelete="SET NULL"), nullable=True
    )

    platform: Mapped[Platform] = relationship(back_populates="credentials")
    versions: Mapped[list[CredentialVersion]] = relationship(
        back_populates="credential_reference",
        foreign_keys="CredentialVersion.credential_reference_id",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="CredentialVersion.version",
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<CredentialReference id={self.id} type={self.credential_type!r} status={self.status!r}>"
