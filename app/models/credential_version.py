"""CredentialVersion model: versioned, encrypted credential payloads.

A credential's *payload* (when storage_type is 'encrypted') is stored here as
AES-256-GCM ciphertext produced by ``app.security.crypto.MasterKeyBox``. The
plaintext never touches the database, logs, or API responses. Each row records
which master-key version encrypted it so keys can be rotated over time.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base

if TYPE_CHECKING:
    from app.models.credential_reference import CredentialReference


class CredentialVersionStatus(enum.StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"


class CredentialVersion(Base):
    __tablename__ = "credential_versions"
    __table_args__ = (
        UniqueConstraint("credential_reference_id", "version", name="uq_credential_versions_reference_version"),
        CheckConstraint("status IN ('active', 'revoked')", name="status_valid"),
        CheckConstraint(
            "encrypted_payload IS NULL OR encryption_key_version IS NOT NULL",
            name="encrypted_payload_requires_key_version",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    credential_reference_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("credential_references.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    # AES-256-GCM ciphertext (nonce || ciphertext || tag), never plaintext.
    encrypted_payload: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    encryption_key_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=CredentialVersionStatus.ACTIVE.value, server_default=CredentialVersionStatus.ACTIVE.value
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, server_default=None
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    credential_reference: Mapped["CredentialReference"] = relationship(
        back_populates="versions"
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<CredentialVersion id={self.id} reference={self.credential_reference_id} "
            f"version={self.version} status={self.status!r}>"
        )
