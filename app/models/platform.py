"""Platform model: a company-owned product or external platform to operate on."""

from __future__ import annotations

import enum
import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.company import Company
    from app.models.credential_reference import CredentialReference


class PlatformType(enum.StrEnum):
    WEBSITE = "website"
    ADMIN_PANEL = "admin_panel"
    API_SERVICE = "api_service"
    SOCIAL_MEDIA = "social_media"
    MOBILE_APP = "mobile_app"
    OTHER = "other"


class PlatformStatus(enum.StrEnum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    MAINTENANCE = "maintenance"
    ARCHIVED = "archived"


class IntegrationType(enum.StrEnum):
    API = "api"
    BROWSER_AUTOMATION = "browser_automation"
    WEBHOOK = "webhook"
    MANUAL = "manual"


class Platform(Base, TimestampMixin):
    __tablename__ = "platforms"
    __table_args__ = (
        UniqueConstraint("company_id", "slug", name="uq_platforms_company_slug"),
        CheckConstraint(
            "platform_type IN ('website', 'admin_panel', 'api_service', "
            "'social_media', 'mobile_app', 'other')",
            name="platform_type_valid",
        ),
        CheckConstraint(
            "status IN ('active', 'inactive', 'maintenance', 'archived')",
            name="status_valid",
        ),
        CheckConstraint(
            "integration_type IN ('api', 'browser_automation', 'webhook', 'manual')",
            name="integration_type_valid",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    platform_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default=PlatformType.OTHER.value, server_default=PlatformType.OTHER.value
    )
    base_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=PlatformStatus.ACTIVE.value, server_default=PlatformStatus.ACTIVE.value
    )
    integration_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default=IntegrationType.API.value, server_default=IntegrationType.API.value
    )
    # Integration configuration (endpoints, feature flags, non-secret options).
    configuration: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    company: Mapped[Company] = relationship(back_populates="platforms")
    # No delete cascade: credential references are protected resources and must
    # be removed explicitly (DB FK uses ON DELETE RESTRICT). passive_deletes
    # tells the ORM to let the database enforce that on parent deletion.
    credentials: Mapped[list[CredentialReference]] = relationship(
        back_populates="platform", passive_deletes=True
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Platform id={self.id} slug={self.slug!r} status={self.status!r}>"
