"""Tenant-owned provider budgets and auditable usage ledger."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base
from app.models.mixins import TimestampMixin


class CompanyProviderBudget(Base, TimestampMixin):
    __tablename__ = "company_provider_budgets"
    __table_args__ = (
        UniqueConstraint("company_id", "provider_id", name="uq_company_provider_budgets_provider"),
        Index("ix_company_provider_budgets_company_enabled", "company_id", "enabled"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    provider_id: Mapped[str] = mapped_column(String(80), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD", server_default="USD")
    monthly_limit_usd: Mapped[Decimal] = mapped_column(Numeric(14, 6), nullable=False)
    per_operation_limit_usd: Mapped[Decimal] = mapped_column(Numeric(14, 6), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))


class ProviderUsageRecord(Base):
    __tablename__ = "provider_usage_records"
    __table_args__ = (
        UniqueConstraint("company_id", "idempotency_key", name="uq_provider_usage_company_idempotency"),
        Index("ix_provider_usage_company_provider_created", "company_id", "provider_id", "created_at"),
        Index("ix_provider_usage_company_status", "company_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    provider_id: Mapped[str] = mapped_column(String(80), nullable=False)
    operation: Mapped[str] = mapped_column(String(100), nullable=False)
    model: Mapped[str | None] = mapped_column(String(160), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="reserved", server_default="reserved")
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    reserved_amount_usd: Mapped[Decimal] = mapped_column(Numeric(14, 6), nullable=False, default=Decimal("0"))
    actual_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 6), nullable=True)
    usage_payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
