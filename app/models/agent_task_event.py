"""Durable, tenant-scoped progress events for agent task timelines."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKeyConstraint, Index, Integer, String, UniqueConstraint, Uuid, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class AgentTaskEvent(Base):
    """Append-only event emitted by a task lifecycle transition."""

    __tablename__ = "agent_task_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["task_id", "company_id"],
            ["agent_tasks.id", "agent_tasks.company_id"],
            ondelete="CASCADE",
            name="fk_agent_task_events_task_company",
        ),
        UniqueConstraint("task_id", "sequence", name="uq_agent_task_events_task_sequence"),
        Index("ix_agent_task_events_company_task_sequence", "company_id", "task_id", "sequence"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    company_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
