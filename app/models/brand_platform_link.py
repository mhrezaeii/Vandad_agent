"""Tenant-safe association between a content brand and its source platforms."""
from __future__ import annotations

import uuid

from sqlalchemy import ForeignKeyConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base
from app.models.mixins import TimestampMixin


class BrandPlatformLink(Base, TimestampMixin):
    __tablename__ = "brand_platform_links"
    __table_args__ = (
        ForeignKeyConstraint(
            ["brand_id", "company_id"],
            ["content_brands.id", "content_brands.company_id"],
            ondelete="CASCADE",
            name="fk_brand_platform_links_brand_company",
        ),
        ForeignKeyConstraint(
            ["platform_id", "company_id"],
            ["platforms.id", "platforms.company_id"],
            ondelete="CASCADE",
            name="fk_brand_platform_links_platform_company",
        ),
    )

    brand_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    platform_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
