"""WorkflowVersion, WorkflowNode and WorkflowEdge models.

A version is an immutable published graph (or the current draft). Nodes and
edges are stored relationally so graphs stay queryable; the (version_id,
node_key) pair is the stable handle used by edges, engine context, and steps.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base

if TYPE_CHECKING:
    from app.models.workflow import Workflow


class WorkflowVersionStatus(enum.StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"
    ARCHIVED = "archived"


class WorkflowVersion(Base):
    __tablename__ = "workflow_versions"
    __table_args__ = (
        UniqueConstraint("workflow_id", "version", name="uq_workflow_versions_workflow_version"),
        CheckConstraint("status IN ('draft', 'published', 'archived')", name="status_valid"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    workflow_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workflows.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=WorkflowVersionStatus.DRAFT.value,
        server_default=WorkflowVersionStatus.DRAFT.value,
    )
    notes: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    workflow: Mapped[Workflow] = relationship(
        back_populates="versions", foreign_keys=[workflow_id]
    )
    nodes: Mapped[list[WorkflowNode]] = relationship(
        back_populates="workflow_version",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="WorkflowNode.node_key",
    )
    edges: Mapped[list[WorkflowEdge]] = relationship(
        back_populates="workflow_version",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<WorkflowVersion id={self.id} workflow={self.workflow_id} v={self.version}>"


class WorkflowNode(Base):
    __tablename__ = "workflow_nodes"
    __table_args__ = (
        UniqueConstraint("version_id", "node_key", name="uq_workflow_nodes_version_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workflow_versions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    node_key: Mapped[str] = mapped_column(String(64), nullable=False)
    node_type: Mapped[str] = mapped_column(String(50), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # Node-type-specific settings, validated before publishing.
    configuration: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    # {param: "nodes.<key>.output.<field>" | "$trigger.<path>" | literal}
    input_mapping: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    # {output_field: "result.<path>"} — project provider result into node output.
    output_mapping: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    # timeout_seconds, max_retries, backoff_seconds, continue_on_error, idempotent
    execution_settings: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    workflow_version: Mapped[WorkflowVersion] = relationship(back_populates="nodes")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<WorkflowNode key={self.node_key!r} type={self.node_type!r}>"


class WorkflowEdge(Base):
    __tablename__ = "workflow_edges"
    __table_args__ = (
        UniqueConstraint("version_id", "from_key", "to_key", name="uq_workflow_edges_version_from_to"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workflow_versions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    from_key: Mapped[str] = mapped_column(String(64), nullable=False)
    to_key: Mapped[str] = mapped_column(String(64), nullable=False)
    # Optional safe-expression condition (evaluated on traversal when the source
    # node completes; NULL edges are unconditional).
    condition_expression: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # Traversal order among a node's outgoing edges.
    edge_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    workflow_version: Mapped[WorkflowVersion] = relationship(back_populates="edges")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<WorkflowEdge {self.from_key!r}->{self.to_key!r}>"
