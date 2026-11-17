"""WorkflowArtifact model: metadata for engine-produced files.

The database stores metadata only; bytes live in the configured artifact
storage backend, addressed by the generated ``storage_key`` (never a client
filename, never a filesystem path exposed through APIs).
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class ArtifactKind(enum.StrEnum):
    SCRIPT = "script"
    AUDIO = "audio"
    VIDEO = "video"
    SUBTITLE = "subtitle"
    IMAGE = "image"
    OTHER = "other"


class WorkflowArtifact(Base):
    __tablename__ = "workflow_artifacts"
    __table_args__ = (
        CheckConstraint("size_bytes >= 0", name="size_non_negative"),
        CheckConstraint("length(sha256) = 64", name="sha256_format"),
        Index("ix_workflow_artifacts_company_created", "company_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # CASCADE: artifact lifetime is bound to its execution.
    execution_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workflow_executions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(50), nullable=False)
    # Backend-agnostic generated key (e.g. executions/<id>/video/<uuid>.mp4).
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    content_type: Mapped[str] = mapped_column(String(255), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    original_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<WorkflowArtifact id={self.id} kind={self.kind!r} size={self.size_bytes}>"
