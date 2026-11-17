"""Budget enforcement for environment-configured OpenAI requests."""

from decimal import Decimal
import uuid

import pytest
from sqlalchemy import select

from app.core.config import Settings
from app.core.exceptions import ConflictError
from app.models import AgentTask, Company, ProviderUsageRecord
from app.services.openai_env_execution_service import OpenAIEnvExecutionService
from app.services.provider_budget_service import ProviderBudgetService


async def test_environment_openai_reservations_enforce_agent_task_spend_ceiling(
    db_session, monkeypatch
):
    company = Company(
        name="Environment GPT Budget",
        slug=f"env-gpt-budget-{uuid.uuid4().hex[:8]}",
    )
    db_session.add(company)
    await db_session.flush()

    task = AgentTask(
        company_id=company.id,
        agent_key="content",
        capability="content.scenario.generate",
        title="Bounded environment GPT task",
        spend_ceiling_usd=Decimal("0.50"),
    )
    db_session.add(task)
    await db_session.flush()

    await ProviderBudgetService(db_session).set_budget(
        company.id,
        "llm_openai",
        monthly_limit_usd=Decimal("5.00"),
        per_operation_limit_usd=Decimal("0.40"),
    )
    monkeypatch.setattr(
        "app.services.openai_env_execution_service.get_settings",
        lambda: Settings(
            openai_api_key="test-key",
            openai_model="test-model",
            _env_file=None,
        ),
    )

    async def fake_completion(**kwargs):
        return {
            "content": "{}",
            "model": "test-model",
            "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
        }

    monkeypatch.setattr(
        "app.services.openai_env_execution_service.openai_chat_completion",
        fake_completion,
    )
    service = OpenAIEnvExecutionService(db_session)

    await service.execute(
        company_id=company.id,
        system="system",
        user="first request",
        operation="content.scenario.generate",
        idempotency_key="env-gpt-task-first",
        task_id=task.id,
    )

    first = await db_session.scalar(
        select(ProviderUsageRecord).where(
            ProviderUsageRecord.company_id == company.id,
            ProviderUsageRecord.idempotency_key == "env-gpt-task-first",
        )
    )
    assert first is not None
    assert first.task_id == task.id
    assert first.status == "unknown"
    assert first.reserved_amount_usd == Decimal("0.400000")
    assert first.usage_payload["total_tokens"] == 30

    with pytest.raises(ConflictError, match="agent task spend ceiling"):
        await service.execute(
            company_id=company.id,
            system="system",
            user="second request",
            operation="platform.knowledge.extract",
            idempotency_key="env-gpt-task-second",
            task_id=task.id,
        )
