"""Workflow model: a company-owned, versioned automation definition."""

from __future__ import annotations

import enum
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, ForeignKey, String, UniqueConstraint, Uuid, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.company import Company
    from app.models.workflow_version import WorkflowVersion


class WorkflowStatus(enum.StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"
    PAUSED = "paused"
    ARCHIVED = "archived"


class Workflow(Base, TimestampMixin):
    __tablename__ = "workflows"
    __table_args__ = (
        UniqueConstraint("company_id", "name", name="uq_workflows_company_name"),
        CheckConstraint(
            "status IN ('draft', 'published', 'paused', 'archived')", name="status_valid"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=WorkflowStatus.DRAFT.value, server_default=WorkflowStatus.DRAFT.value
    )
    # Immutable snapshot executed by the engine; NULL while never published.
    # SET NULL so deleting a version never destroys workflow history.
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("workflow_versions.id", ondelete="SET NULL"), nullable=True
    )
    # Default execution limits for new executions of this workflow.
    default_execution_settings: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    company: Mapped[Company] = relationship()
    versions: Mapped[list[WorkflowVersion]] = relationship(
        back_populates="workflow",
        foreign_keys="WorkflowVersion.workflow_id",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="WorkflowVersion.version",
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Workflow id={self.id} name={self.name!r} status={self.status!r}>"
