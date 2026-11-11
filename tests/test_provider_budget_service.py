"""Company provider budget reservations and usage settlement."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from app.core.exceptions import ConflictError
from app.models import Company
from app.services.provider_budget_service import ProviderBudgetService, parse_reported_cost


async def test_budget_reserves_per_operation_and_settles_reported_usd_cost(db_session):
    company = Company(name="Budget Owner", slug=f"budget-owner-{uuid.uuid4().hex[:8]}")
    db_session.add(company)
    await db_session.flush()
    service = ProviderBudgetService(db_session)
    await service.set_budget(
        company.id,
        "llm_openai",
        monthly_limit_usd=Decimal("5.00"),
        per_operation_limit_usd=Decimal("2.00"),
    )

    reservation = await service.reserve(
        company.id,
        "llm_openai",
        "content.scenario.generate",
        idempotency_key=f"usage-{uuid.uuid4()}",
        model="gpt-compatible",
        require_budget=True,
    )
    assert reservation is not None
    assert reservation.status == "reserved"
    assert reservation.reserved_amount_usd == Decimal("2.000000")

    await service.settle(
        reservation,
        usage={"cost_usd": "1.25", "total_tokens": 120, "model": "model-a"},
    )
    assert reservation.status == "settled"
    assert reservation.actual_cost_usd == Decimal("1.25")
    assert reservation.reserved_amount_usd == Decimal("0")
    assert reservation.usage_payload["total_tokens"] == 120


async def test_unknown_provider_cost_remains_reserved(db_session):
    company = Company(name="Unknown Cost", slug=f"unknown-cost-{uuid.uuid4().hex[:8]}")
    db_session.add(company)
    await db_session.flush()
    service = ProviderBudgetService(db_session)
    await service.set_budget(
        company.id,
        "llm_openai",
        monthly_limit_usd=Decimal("3.00"),
        per_operation_limit_usd=Decimal("1.00"),
    )
    reservation = await service.reserve(
        company.id,
        "llm_openai",
        "knowledge.extract",
        idempotency_key=f"unknown-{uuid.uuid4()}",
        require_budget=True,
    )
    assert reservation is not None

    await service.settle(reservation, usage={"total_tokens": 50})
    assert reservation.status == "unknown"
    assert reservation.actual_cost_usd is None
    assert reservation.reserved_amount_usd == Decimal("1.000000")


async def test_budget_rejects_spend_above_monthly_limit(db_session):
    company = Company(name="Budget Limit", slug=f"budget-limit-{uuid.uuid4().hex[:8]}")
    db_session.add(company)
    await db_session.flush()
    service = ProviderBudgetService(db_session)
    await service.set_budget(
        company.id,
        "llm_openai",
        monthly_limit_usd=Decimal("2.00"),
        per_operation_limit_usd=Decimal("1.50"),
    )
    first = await service.reserve(
        company.id, "llm_openai", "first", idempotency_key=f"first-{uuid.uuid4()}"
    )
    assert first is not None
    await service.settle(first, usage={})  # unknown cost keeps the full reservation

    with pytest.raises(ConflictError, match="remaining monthly budget"):
        await service.reserve(
            company.id, "llm_openai", "second", idempotency_key=f"second-{uuid.uuid4()}"
        )


async def test_paid_operation_fails_closed_without_company_budget(db_session):
    company = Company(name="No Budget", slug=f"no-budget-{uuid.uuid4().hex[:8]}")
    db_session.add(company)
    await db_session.flush()

    with pytest.raises(ConflictError, match="budget is required"):
        await ProviderBudgetService(db_session).reserve(
            company.id,
            "apimaster",
            "video.scene.generate",
            idempotency_key=f"paid-video-{uuid.uuid4()}",
            require_budget=True,
        )


async def test_duplicate_paid_operation_is_blocked(db_session):
    company = Company(name="No Duplicate", slug=f"no-duplicate-{uuid.uuid4().hex[:8]}")
    db_session.add(company)
    await db_session.flush()
    service = ProviderBudgetService(db_session)
    await service.set_budget(
        company.id,
        "llm_openai",
        monthly_limit_usd=Decimal("5.00"),
        per_operation_limit_usd=Decimal("1.00"),
    )
    key = f"same-call-{uuid.uuid4()}"
    await service.reserve(company.id, "llm_openai", "complete", idempotency_key=key)

    with pytest.raises(ConflictError, match="duplicate paid execution"):
        await service.reserve(company.id, "llm_openai", "complete", idempotency_key=key)


def test_cost_parser_only_accepts_usd_denominated_values():
    assert parse_reported_cost({"cost_usd": "0.42"}) == Decimal("0.42")
    assert parse_reported_cost({"cost": "0.42", "currency": "USD"}) == Decimal("0.42")
    assert parse_reported_cost({"cost": "0.42", "currency": "EUR"}) is None
    assert parse_reported_cost({"cost": "0.42"}) is None
