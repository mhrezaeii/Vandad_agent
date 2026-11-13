"""Ordered LLM fallback routing and budget-safety tests."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.core.exceptions import NotFoundError
from app.integrations.providers.base import (
    ProviderPermanentError,
    ProviderResult,
    ProviderTransientError,
)
from app.models import (
    Company,
    Platform,
    PlatformConnection,
    ProviderUsageRecord,
)
from app.schemas.connection import ConnectionCreate, ConnectionUpdate
from app.services.llm_execution_service import LlmExecutionService
from app.services.provider_budget_service import ProviderBudgetService


async def _create_connection(
    db_session,
    company,
    platform,
    *,
    name: str,
    model: str,
    llm_fallbacks: list[dict[str, str]] | None = None,
) -> PlatformConnection:
    configuration = {
        "url": "https://llm.example.test/v1",
        "model": model,
    }
    if llm_fallbacks is not None:
        configuration["llm_fallbacks"] = llm_fallbacks
    connection = PlatformConnection(
        company_id=company.id,
        platform_id=platform.id,
        name=name,
        provider="llm_openai",
        status="connected",
        configuration=configuration,
    )
    db_session.add(connection)
    await db_session.flush()
    return connection


async def _set_budget(db_session, company_id):
    await ProviderBudgetService(db_session).set_budget(
        company_id,
        "llm_openai",
        monthly_limit_usd=Decimal("5.00"),
        per_operation_limit_usd=Decimal("1.00"),
    )


async def test_ordered_model_fallback_uses_one_budget_reservation(
    db_session, company_and_platform, engine_pair, key_box, monkeypatch
):
    company, platform = company_and_platform
    connection = await _create_connection(
        db_session,
        company,
        platform,
        name="primary-llm",
        model="primary-model",
        llm_fallbacks=[{"model": "backup-model"}],
    )
    await _set_budget(db_session, company.id)
    await db_session.commit()

    registry, executor = engine_pair
    adapter = registry.get("llm_openai")
    calls: list[str] = []

    async def fake_execute(context):
        model = context.configuration["model"]
        calls.append(model)
        if model == "primary-model":
            raise ProviderPermanentError("Primary model unavailable.", code="llm_http_404")
        return ProviderResult(
            provider_id="llm_openai",
            success=True,
            message="Fallback model completed the request.",
            metadata={
                "content": "generated",
                "model": model,
                "usage": {"cost_usd": "0.25", "currency": "USD"},
            },
        )

    monkeypatch.setattr(adapter, "execute", fake_execute)
    key = f"llm-fallback-{uuid.uuid4()}"
    result = await LlmExecutionService(
        db_session, registry, executor, key_box
    ).execute(
        company_id=company.id,
        connection_id=connection.id,
        configuration_overrides={"params": {"system": "system", "user": "user"}},
        timeout_seconds=5,
        operation="test.llm.fallback",
        idempotency_key=key,
    )
    await db_session.commit()

    assert calls == ["primary-model", "backup-model"]
    assert result.metadata["model"] == "backup-model"
    assert result.metadata["routing_attempts"] == 2
    assert result.metadata["fallback_used"] is True
    usage = await db_session.scalar(
        select(ProviderUsageRecord).where(ProviderUsageRecord.idempotency_key == key)
    )
    assert usage is not None
    assert usage.status == "settled"
    assert usage.actual_cost_usd == Decimal("0.25")


async def test_ordered_fallback_can_switch_to_another_tenant_connection(
    db_session, company_and_platform, engine_pair, key_box, monkeypatch
):
    company, platform = company_and_platform
    backup = await _create_connection(
        db_session,
        company,
        platform,
        name="backup-llm",
        model="backup-connection-model",
    )
    primary = await _create_connection(
        db_session,
        company,
        platform,
        name="primary-llm",
        model="primary-model",
        llm_fallbacks=[{"connection_id": str(backup.id)}],
    )
    await _set_budget(db_session, company.id)
    await db_session.commit()

    registry, executor = engine_pair
    adapter = registry.get("llm_openai")
    calls: list[uuid.UUID] = []

    async def fake_execute(context):
        calls.append(context.connection_id)
        if context.connection_id == primary.id:
            raise ProviderPermanentError("Primary credential rejected.", code="llm_http_401")
        return ProviderResult(
            provider_id="llm_openai",
            success=True,
            message="Backup connection completed the request.",
            metadata={
                "content": "generated",
                "model": context.configuration["model"],
                "usage": {"cost_usd": "0.10", "currency": "USD"},
            },
        )

    monkeypatch.setattr(adapter, "execute", fake_execute)
    result = await LlmExecutionService(
        db_session, registry, executor, key_box
    ).execute(
        company_id=company.id,
        connection_id=primary.id,
        configuration_overrides={"params": {"system": "system", "user": "user"}},
        timeout_seconds=5,
        operation="test.llm.connection_fallback",
        idempotency_key=f"llm-connection-fallback-{uuid.uuid4()}",
    )
    await db_session.commit()

    assert calls == [primary.id, backup.id]
    assert result.metadata["model"] == "backup-connection-model"
    assert result.metadata["fallback_used"] is True


async def test_ambiguous_timeout_does_not_try_fallback_and_keeps_budget_unknown(
    db_session, company_and_platform, engine_pair, key_box, monkeypatch
):
    company, platform = company_and_platform
    connection = await _create_connection(
        db_session,
        company,
        platform,
        name="primary-llm",
        model="primary-model",
        llm_fallbacks=[{"model": "backup-model"}],
    )
    await _set_budget(db_session, company.id)
    await db_session.commit()

    registry, executor = engine_pair
    adapter = registry.get("llm_openai")
    calls: list[str] = []

    async def fake_execute(context):
        calls.append(context.configuration["model"])
        raise ProviderTransientError(
            "The provider response timed out after dispatch.",
            code="provider_timeout",
        )

    monkeypatch.setattr(adapter, "execute", fake_execute)
    key = f"llm-ambiguous-{uuid.uuid4()}"
    with pytest.raises(ProviderPermanentError, match="timed out"):
        await LlmExecutionService(
            db_session, registry, executor, key_box
        ).execute(
            company_id=company.id,
            connection_id=connection.id,
            configuration_overrides={"params": {"system": "system", "user": "user"}},
            timeout_seconds=5,
            operation="test.llm.ambiguous",
            idempotency_key=key,
        )
    await db_session.commit()

    assert calls == ["primary-model"]
    usage = await db_session.scalar(
        select(ProviderUsageRecord).where(ProviderUsageRecord.idempotency_key == key)
    )
    assert usage is not None
    assert usage.status == "unknown"
    assert usage.reserved_amount_usd == Decimal("1.000000")


async def test_all_definitive_rejections_release_the_logical_reservation(
    db_session, company_and_platform, engine_pair, key_box, monkeypatch
):
    company, platform = company_and_platform
    connection = await _create_connection(
        db_session,
        company,
        platform,
        name="primary-llm",
        model="primary-model",
        llm_fallbacks=[{"model": "backup-model"}],
    )
    await _set_budget(db_session, company.id)
    await db_session.commit()

    registry, executor = engine_pair
    adapter = registry.get("llm_openai")
    calls: list[str] = []

    async def fake_execute(context):
        calls.append(context.configuration["model"])
        raise ProviderPermanentError("Model not found.", code="llm_http_404")

    monkeypatch.setattr(adapter, "execute", fake_execute)
    key = f"llm-rejected-{uuid.uuid4()}"
    with pytest.raises(ProviderPermanentError, match="Model not found"):
        await LlmExecutionService(
            db_session, registry, executor, key_box
        ).execute(
            company_id=company.id,
            connection_id=connection.id,
            configuration_overrides={"params": {"system": "system", "user": "user"}},
            timeout_seconds=5,
            operation="test.llm.rejected",
            idempotency_key=key,
        )
    await db_session.commit()

    assert calls == ["primary-model", "backup-model"]
    usage = await db_session.scalar(
        select(ProviderUsageRecord).where(ProviderUsageRecord.idempotency_key == key)
    )
    assert usage is not None
    assert usage.status == "settled"
    assert usage.actual_cost_usd == Decimal("0")


async def test_fallback_connection_is_tenant_scoped(
    db_session, company_and_platform, engine_pair, key_box
):
    company, platform = company_and_platform
    stranger = Company(name="Other Tenant", slug=f"other-tenant-{uuid.uuid4().hex[:8]}")
    db_session.add(stranger)
    await db_session.flush()
    foreign_platform = Platform(
        company_id=stranger.id,
        name="Foreign Platform",
        slug=f"foreign-platform-{uuid.uuid4().hex[:8]}",
        platform_type="admin_panel",
    )
    db_session.add(foreign_platform)
    await db_session.flush()
    foreign_connection = await _create_connection(
        db_session,
        stranger,
        foreign_platform,
        name="foreign-llm",
        model="foreign-model",
    )
    primary = await _create_connection(
        db_session,
        company,
        platform,
        name="primary-llm",
        model="primary-model",
        llm_fallbacks=[{"connection_id": str(foreign_connection.id)}],
    )
    await _set_budget(db_session, company.id)
    await db_session.commit()

    registry, executor = engine_pair
    with pytest.raises(NotFoundError, match="LLM connection not found"):
        await LlmExecutionService(
            db_session, registry, executor, key_box
        ).execute(
            company_id=company.id,
            connection_id=primary.id,
            configuration_overrides={"params": {"system": "system", "user": "user"}},
            timeout_seconds=5,
            operation="test.llm.foreign_fallback",
            idempotency_key=f"llm-foreign-{uuid.uuid4()}",
        )



def test_connection_schema_accepts_ordered_llm_fallback_configuration():
    payload = ConnectionCreate(
        platform_id=uuid.uuid4(),
        name="Primary LLM",
        provider="llm_openai",
        configuration={
            "url": "https://llm.example.test/v1",
            "model": "primary-model",
            "llm_fallbacks": [{"model": "backup-model"}],
        },
    )
    assert payload.configuration["llm_fallbacks"] == [{"model": "backup-model"}]

    update = ConnectionUpdate(
        configuration={"llm_fallbacks": [{"connection_id": str(uuid.uuid4())}]}
    )
    assert update.configuration is not None
    assert len(update.configuration["llm_fallbacks"]) == 1


@pytest.mark.parametrize(
    "fallbacks",
    [
        "not-a-list",
        [{}],
        [{"model": "  "}],
        [{"connection_id": "not-a-uuid"}],
        [{"model": "model", "unexpected": "field"}],
        [{"model": f"model-{index}"} for index in range(6)],
    ],
)
def test_connection_schema_rejects_invalid_llm_fallback_configuration(fallbacks):
    with pytest.raises(ValidationError):
        ConnectionCreate(
            platform_id=uuid.uuid4(),
            name="Primary LLM",
            provider="llm_openai",
            configuration={"llm_fallbacks": fallbacks},
        )
