"""Provider budget reservation and actual-usage settlement."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError
from app.models import ActorType, CompanyProviderBudget, ProviderUsageRecord
from app.services.audit_service import AuditService


def parse_reported_cost(payload: dict[str, Any]) -> Decimal | None:
    """Return a cost only when the provider explicitly reports a USD amount."""
    candidates = [payload.get("cost_usd"), payload.get("total_cost_usd")]
    if str(payload.get("currency") or "").upper() == "USD":
        candidates.extend((payload.get("cost"), payload.get("total_cost")))
    for value in candidates:
        if value is None or isinstance(value, bool):
            continue
        try:
            amount = Decimal(str(value))
        except (InvalidOperation, ValueError):
            continue
        if amount.is_finite() and amount >= 0:
            return amount
    return None


def _safe_usage(payload: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "prompt_tokens", "completion_tokens", "total_tokens",
        "input_tokens", "output_tokens", "total_cost", "cost",
        "total_cost_usd", "cost_usd", "model", "latency_ms",
        "attempts", "provider_request_id", "currency",
    }
    return {key: value for key, value in payload.items() if key in allowed}


class ProviderBudgetService:
    """Reserve a configured per-operation ceiling and settle from provider usage.

    If a provider does not report cost, the reservation remains charged against
    the monthly budget as an unknown-cost operation. Operators must reconcile it.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.audit = AuditService(session)

    async def reserve(
        self, company_id: uuid.UUID, provider_id: str, operation: str, *,
        idempotency_key: str, model: str | None = None, require_budget: bool = False,
    ) -> ProviderUsageRecord | None:
        existing = await self.session.scalar(
            select(ProviderUsageRecord).where(
                ProviderUsageRecord.company_id == company_id,
                ProviderUsageRecord.idempotency_key == idempotency_key,
            ).with_for_update()
        )
        if existing is not None:
            if existing.provider_id != provider_id or existing.operation != operation:
                raise ConflictError("Provider usage idempotency key was reused for a different operation.")
            raise ConflictError(
                "This provider operation already has a usage reservation; duplicate paid execution is blocked."
            )

        budget = await self.session.scalar(
            select(CompanyProviderBudget).where(
                CompanyProviderBudget.company_id == company_id,
                CompanyProviderBudget.provider_id == provider_id,
            ).with_for_update()
        )
        # Serialize same-key reservations behind the budget row lock, then
        # recheck so concurrent requests cannot double-reserve the same call.
        existing = await self.session.scalar(
            select(ProviderUsageRecord).where(
                ProviderUsageRecord.company_id == company_id,
                ProviderUsageRecord.idempotency_key == idempotency_key,
            ).with_for_update()
        )
        if existing is not None:
            raise ConflictError("This provider operation already has a usage reservation; duplicate paid execution is blocked.")
        if budget is None:
            if require_budget:
                raise ConflictError("A company provider budget is required before this paid operation.")
            return None
        if not budget.enabled:
            raise ConflictError("This provider is disabled by the company budget policy.")

        now = datetime.now(UTC)
        month_start = datetime(now.year, now.month, 1, tzinfo=UTC)
        rows = list((await self.session.scalars(
            select(ProviderUsageRecord).where(
                ProviderUsageRecord.company_id == company_id,
                ProviderUsageRecord.provider_id == provider_id,
                ProviderUsageRecord.created_at >= month_start,
                ProviderUsageRecord.status.in_(("reserved", "settled", "unknown")),
            )
        )).all())
        committed = sum(
            (row.actual_cost_usd if row.actual_cost_usd is not None else row.reserved_amount_usd)
            for row in rows
        )
        ceiling = budget.per_operation_limit_usd
        if ceiling <= 0 or committed + ceiling > budget.monthly_limit_usd:
            raise ConflictError("The provider operation exceeds the remaining monthly budget.")

        record = ProviderUsageRecord(
            company_id=company_id,
            provider_id=provider_id,
            operation=operation,
            model=model,
            status="reserved",
            idempotency_key=idempotency_key,
            reserved_amount_usd=ceiling,
            usage_payload={"budget_reservation": "per_operation_ceiling_usd"},
        )
        self.session.add(record)
        await self.session.flush()
        return record

    async def settle(
        self, record: ProviderUsageRecord | None, *,
        usage: dict[str, Any] | None = None, error_code: str | None = None,
    ) -> ProviderUsageRecord | None:
        if record is None:
            return None
        now = datetime.now(UTC)
        if error_code:
            # A provider error may occur after the vendor accepted/billed the
            # request. Keep the ceiling reserved until an operator reconciles it.
            record.status = "unknown"
            record.error_code = error_code[:100]
            record.usage_payload = {"provider_error_code": record.error_code}
            record.completed_at = now
        else:
            safe = _safe_usage(usage or {})
            actual = parse_reported_cost(safe)
            record.usage_payload = safe
            record.model = str(safe.get("model") or record.model or "")[:160] or None
            record.completed_at = now
            if actual is None:
                record.status = "unknown"
                record.actual_cost_usd = None
                # Keep reservation charged: unknown must not become free spend.
            else:
                record.status = "settled"
                record.actual_cost_usd = actual
                record.reserved_amount_usd = Decimal("0")
        await self.session.flush()
        return record

    async def reconcile_usage(
        self, company_id: uuid.UUID, record_id: uuid.UUID, *,
        actual_cost_usd: Decimal, note: str, reconciled_by: str,
    ) -> ProviderUsageRecord:
        record = await self.session.scalar(
            select(ProviderUsageRecord).where(
                ProviderUsageRecord.id == record_id,
                ProviderUsageRecord.company_id == company_id,
            ).with_for_update()
        )
        if record is None:
            raise NotFoundError("Provider usage record not found.")
        if record.status not in {"unknown", "reserved"}:
            raise ConflictError("Only unknown or reserved usage records can be reconciled.")
        if actual_cost_usd < 0:
            raise ConflictError("Actual provider cost cannot be negative.")
        record.actual_cost_usd = actual_cost_usd
        record.reserved_amount_usd = Decimal("0")
        record.status = "settled"
        record.completed_at = datetime.now(UTC)
        record.usage_payload = {
            **(record.usage_payload or {}),
            "operator_reconciled": True,
            "reconciliation_note": note[:500],
        }
        await self.session.flush()
        self.audit.record(
            event_type="provider.usage.reconciled",
            action="reconcile_provider_usage",
            actor_type=ActorType.USER,
            actor_id=reconciled_by,
            company_id=company_id,
            resource_type="provider_usage_record",
            resource_id=record.id,
            metadata={
                "provider_id": record.provider_id,
                "actual_cost_usd": str(actual_cost_usd),
                "note": note[:500],
            },
        )
        return record

    async def assert_budget_enabled(
        self, company_id: uuid.UUID, provider_id: str
    ) -> CompanyProviderBudget:
        budget = await self.session.scalar(
            select(CompanyProviderBudget).where(
                CompanyProviderBudget.company_id == company_id,
                CompanyProviderBudget.provider_id == provider_id,
            )
        )
        if budget is None or not budget.enabled:
            raise ConflictError(
                "A company provider budget must be configured and enabled before this paid operation."
            )
        return budget

    async def set_budget(
        self, company_id: uuid.UUID, provider_id: str, *,
        monthly_limit_usd: Decimal, per_operation_limit_usd: Decimal, enabled: bool = True,
    ) -> CompanyProviderBudget:
        if monthly_limit_usd <= 0 or per_operation_limit_usd <= 0:
            raise ConflictError("Budget limits must be positive amounts.")
        if per_operation_limit_usd > monthly_limit_usd:
            raise ConflictError("Per-operation limit cannot exceed the monthly limit.")
        budget = await self.session.scalar(
            select(CompanyProviderBudget).where(
                CompanyProviderBudget.company_id == company_id,
                CompanyProviderBudget.provider_id == provider_id,
            ).with_for_update()
        )
        if budget is None:
            budget = CompanyProviderBudget(
                company_id=company_id,
                provider_id=provider_id,
                monthly_limit_usd=monthly_limit_usd,
                per_operation_limit_usd=per_operation_limit_usd,
                enabled=enabled,
            )
            self.session.add(budget)
        else:
            budget.monthly_limit_usd = monthly_limit_usd
            budget.per_operation_limit_usd = per_operation_limit_usd
            budget.enabled = enabled
        await self.session.flush()
        return budget

    async def list_budgets(self, company_id: uuid.UUID) -> list[CompanyProviderBudget]:
        rows = await self.session.scalars(
            select(CompanyProviderBudget).where(
                CompanyProviderBudget.company_id == company_id
            ).order_by(CompanyProviderBudget.provider_id)
        )
        return list(rows.all())

    async def list_usage(
        self, company_id: uuid.UUID, *, provider_id: str | None = None, limit: int = 100
    ) -> list[ProviderUsageRecord]:
        statement = select(ProviderUsageRecord).where(
            ProviderUsageRecord.company_id == company_id
        )
        if provider_id:
            statement = statement.where(ProviderUsageRecord.provider_id == provider_id)
        rows = await self.session.scalars(
            statement.order_by(ProviderUsageRecord.created_at.desc()).limit(limit)
        )
        return list(rows.all())
