"""Company provider budget reservations and usage settlement."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from app.core.exceptions import ConflictError
from app.models import AgentTask, Company, ProviderUsageRecord
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


async def test_ambiguous_provider_failure_keeps_reservation_and_safe_telemetry(db_session):
    company = Company(name="Failed Usage", slug=f"failed-usage-{uuid.uuid4().hex[:8]}")
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
        "scenario",
        idempotency_key=f"failed-{uuid.uuid4()}",
        require_budget=True,
    )
    await service.settle(
        reservation,
        error_code="provider_timeout",
        usage={"latency_ms": 245, "attempts": 3, "api_key": "never-store"},
    )
    assert reservation.status == "unknown"
    assert reservation.reserved_amount_usd == Decimal("1.000000")
    assert reservation.usage_payload == {
        "latency_ms": 245,
        "attempts": 3,
        "provider_error_code": "provider_timeout",
    }


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


async def test_task_spend_ceiling_aggregates_provider_reservations(db_session):
    company = Company(name="Task Budget", slug=f"task-budget-{uuid.uuid4().hex[:8]}")
    db_session.add(company)
    await db_session.flush()
    task = AgentTask(
        company_id=company.id,
        agent_key="content",
        capability="content.scenario.generate",
        title="Bounded task",
        spend_ceiling_usd=Decimal("2.50"),
    )
    db_session.add(task)
    await db_session.flush()
    service = ProviderBudgetService(db_session)
    for provider in ("llm_openai", "apimaster"):
        await service.set_budget(
            company.id, provider,
            monthly_limit_usd=Decimal("10.00"),
            per_operation_limit_usd=Decimal("1.50"),
        )

    first = await service.reserve(
        company.id, "llm_openai", "scenario",
        idempotency_key=f"task-first-{uuid.uuid4()}",
        require_budget=True, task_id=task.id,
    )
    assert first is not None and first.task_id == task.id

    with pytest.raises(ConflictError, match="agent task spend ceiling"):
        await service.reserve(
            company.id, "apimaster", "video",
            idempotency_key=f"task-second-{uuid.uuid4()}",
            require_budget=True, task_id=task.id,
        )


async def test_task_budget_is_tenant_scoped(db_session):
    owner = Company(name="Task Budget Owner", slug=f"task-owner-{uuid.uuid4().hex[:8]}")
    stranger = Company(name="Task Budget Stranger", slug=f"task-stranger-{uuid.uuid4().hex[:8]}")
    db_session.add_all([owner, stranger])
    await db_session.flush()
    task = AgentTask(
        company_id=owner.id, agent_key="content", capability="content.scenario.generate",
        title="Owner task", spend_ceiling_usd=Decimal("1.00"),
    )
    db_session.add(task)
    await db_session.flush()
    service = ProviderBudgetService(db_session)
    await service.set_budget(
        stranger.id, "llm_openai", monthly_limit_usd=Decimal("5.00"),
        per_operation_limit_usd=Decimal("1.00"),
    )
    with pytest.raises(ConflictError, match="task budget context"):
        await service.reserve(
            stranger.id, "llm_openai", "scenario",
            idempotency_key=f"foreign-task-{uuid.uuid4()}",
            require_budget=True, task_id=task.id,
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



async def test_budget_api_is_company_scoped(client, db_session):
    owner = Company(name="Budget API Owner", slug=f"budget-api-owner-{uuid.uuid4().hex[:8]}")
    stranger = Company(name="Budget API Stranger", slug=f"budget-api-stranger-{uuid.uuid4().hex[:8]}")
    db_session.add_all([owner, stranger])
    await db_session.commit()

    owner_headers = {"X-Company-ID": str(owner.id)}
    stranger_headers = {"X-Company-ID": str(stranger.id)}
    created = await client.put(
        "/api/v1/providers/budgets/llm_openai",
        headers=owner_headers,
        json={
            "monthly_limit_usd": "10.00",
            "per_operation_limit_usd": "2.00",
            "enabled": True,
        },
    )
    assert created.status_code == 200, created.text
    assert created.json()["provider_id"] == "llm_openai"

    own_list = await client.get("/api/v1/providers/budgets", headers=owner_headers)
    assert own_list.status_code == 200
    assert len(own_list.json()) == 1

    foreign_list = await client.get("/api/v1/providers/budgets", headers=stranger_headers)
    assert foreign_list.status_code == 200
    assert foreign_list.json() == []


async def test_budget_api_rejects_unregistered_provider(client, company_and_platform):
    company, _ = company_and_platform
    response = await client.put(
        "/api/v1/providers/budgets/not-a-provider",
        headers={"X-Company-ID": str(company.id)},
        json={
            "monthly_limit_usd": "10.00",
            "per_operation_limit_usd": "2.00",
            "enabled": True,
        },
    )
    assert response.status_code == 404



async def test_unknown_usage_can_be_audited_and_reconciled(db_session):
    company = Company(name="Reconciliation", slug=f"reconcile-{uuid.uuid4().hex[:8]}")
    db_session.add(company)
    await db_session.flush()
    service = ProviderBudgetService(db_session)
    await service.set_budget(
        company.id,
        "apimaster",
        monthly_limit_usd=Decimal("20.00"),
        per_operation_limit_usd=Decimal("5.00"),
    )
    reservation = await service.reserve(
        company.id,
        "apimaster",
        "content.scene.generate_video",
        idempotency_key=f"reconcile-{uuid.uuid4()}",
        require_budget=True,
    )
    assert reservation is not None
    await service.settle(reservation, usage={})
    assert reservation.status == "unknown"

    reconciled = await service.reconcile_usage(
        company.id,
        reservation.id,
        actual_cost_usd=Decimal("3.75"),
        note="Verified against the provider invoice.",
        reconciled_by="operator-1",
    )
    assert reconciled.status == "settled"
    assert reconciled.actual_cost_usd == Decimal("3.75")
    assert reconciled.reserved_amount_usd == Decimal("0")
    assert reconciled.usage_payload["operator_reconciled"] is True



async def test_provider_telemetry_is_company_scoped_and_aggregates_latency(db_session):
    owner = Company(name="Telemetry Owner", slug=f"telemetry-owner-{uuid.uuid4().hex[:8]}")
    stranger = Company(name="Telemetry Stranger", slug=f"telemetry-stranger-{uuid.uuid4().hex[:8]}")
    db_session.add_all([owner, stranger])
    await db_session.flush()
    db_session.add_all([
        ProviderUsageRecord(
            company_id=owner.id,
            provider_id="llm_openai",
            operation="scenario",
            status="settled",
            idempotency_key=f"telemetry-ok-{uuid.uuid4()}",
            reserved_amount_usd=Decimal("0"),
            actual_cost_usd=Decimal("0.25"),
            usage_payload={"latency_ms": 120, "attempts": 1},
        ),
        ProviderUsageRecord(
            company_id=owner.id,
            provider_id="llm_openai",
            operation="scenario",
            status="unknown",
            idempotency_key=f"telemetry-error-{uuid.uuid4()}",
            reserved_amount_usd=Decimal("1.00"),
            usage_payload={"latency_ms": 300, "attempts": 2},
            error_code="provider_timeout",
        ),
        ProviderUsageRecord(
            company_id=owner.id,
            provider_id="llm_openai",
            operation="scenario",
            status="reserved",
            idempotency_key=f"telemetry-pending-{uuid.uuid4()}",
            reserved_amount_usd=Decimal("1.00"),
            usage_payload={},
        ),
        ProviderUsageRecord(
            company_id=stranger.id,
            provider_id="llm_openai",
            operation="scenario",
            status="settled",
            idempotency_key=f"telemetry-foreign-{uuid.uuid4()}",
            reserved_amount_usd=Decimal("0"),
            actual_cost_usd=Decimal("0.10"),
            usage_payload={"latency_ms": 10},
        ),
    ])
    await db_session.flush()

    metrics = await ProviderBudgetService(db_session).telemetry(owner.id)
    assert len(metrics) == 1
    assert metrics[0]["provider_id"] == "llm_openai"
    assert metrics[0]["calls_30d"] == 3
    assert metrics[0]["errors_30d"] == 1
    assert metrics[0]["pending_30d"] == 1
    assert metrics[0]["success_rate"] == 0.5
    assert metrics[0]["average_latency_ms"] == 210.0
