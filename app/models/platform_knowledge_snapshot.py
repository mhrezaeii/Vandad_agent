"""AI-extracted, source-grounded knowledge snapshot for a company platform."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, String, Uuid, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base
from app.models.mixins import TimestampMixin


class PlatformKnowledgeSnapshot(Base, TimestampMixin):
    __tablename__ = "platform_knowledge_snapshots"
    __table_args__ = (
        Index("ix_platform_knowledge_snapshots_company_platform", "company_id", "platform_id"),
        Index("ix_platform_knowledge_snapshots_platform_created", "platform_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    platform_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("platforms.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_document_ids: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    source_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    knowledge_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    extracted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
