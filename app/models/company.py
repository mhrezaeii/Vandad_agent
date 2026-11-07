"""Company model: the top-level tenant owning platforms and credentials."""

from __future__ import annotations

import enum
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.platform import Platform


class CompanyStatus(enum.StrEnum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    SUSPENDED = "suspended"


class Company(Base, TimestampMixin):
    __tablename__ = "companies"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'inactive', 'suspended')", name="status_valid"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=CompanyStatus.ACTIVE.value, server_default=CompanyStatus.ACTIVE.value
    )

    platforms: Mapped[list[Platform]] = relationship(
        back_populates="company", cascade="all, delete-orphan", passive_deletes=True
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Company id={self.id} slug={self.slug!r} status={self.status!r}>"
