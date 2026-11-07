"""CredentialReference model: pointers to secrets, never the secrets themselves.

``secret_reference`` holds a locator (e.g. ``vault://platform-x/api-key``)
resolvable by an external secret store. Raw passwords and API keys must never
be written to this table; format is enforced by ``app.security.redaction``.
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


class CredentialReference(Base, TimestampMixin):
    __tablename__ = "credential_references"
    __table_args__ = (
        UniqueConstraint("platform_id", "secret_reference", name="uq_credential_references_platform_reference"),
        CheckConstraint(
            "credential_type IN ('api_key', 'oauth2', 'basic_auth', 'browser_session', 'token')",
            name="credential_type_valid",
        ),
        CheckConstraint("status IN ('active', 'revoked', 'expired')", name="status_valid"),
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

    platform: Mapped[Platform] = relationship(back_populates="credentials")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<CredentialReference id={self.id} type={self.credential_type!r} status={self.status!r}>"
