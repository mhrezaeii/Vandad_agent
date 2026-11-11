"""Agent dispatcher and durable outbox integration tests."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.agents import build_default_agent_registry
from app.agents.runtime import AgentDispatcher, AgentExecutionContext, MonitoringAgentHandler
from app.core.exceptions import ConflictError
from app.models import AgentTask, AgentTaskOutbox, CompanyAgent
from app.services.agent_task_service import AgentTaskService
from app.workers.agent_worker import AgentTaskWorker


class _FakeMonitoringHandler:
    agent_key = "monitoring"
    capabilities = frozenset({"platform.discover"})

    async def execute(self, capability, context):
        return {"platform_id": str(context.payload["platform_id"]), "scanned": 1}


async def test_dispatcher_routes_only_registered_agent_capabilities():
    registry = build_default_agent_registry()
    dispatcher = AgentDispatcher(registry, [_FakeMonitoringHandler()])
    context = AgentExecutionContext(
        company_id=uuid.uuid4(),
        task_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        worker_id="test-worker",
        payload={"platform_id": str(uuid.uuid4())},
        session_factory=None,
        provider_registry=None,
        provider_executor=None,
        master_key_box=None,
    )
    result = await dispatcher.dispatch("monitoring", "platform.discover", context)
    assert result["scanned"] == 1

    with pytest.raises(ConflictError):
        await dispatcher.dispatch("monitoring", "platform.delete", context)


async def test_dispatcher_rejects_unready_agent_handler():
    from app.agents import AgentRegistry
    from app.agents.definitions import AgentDefinition

    registry = AgentRegistry((AgentDefinition(
        key="not_ready", name="Not Ready", description="test",
        capabilities=("test.run",), maturity="partial", dispatch_ready=False,
    ),))

    class Handler:
        agent_key = "not_ready"
        capabilities = frozenset({"test.run"})

        async def execute(self, capability, context):
            return {}

    with pytest.raises(ValueError):
        AgentDispatcher(registry, [Handler()])


async def test_monitoring_task_is_atomically_enqueued_and_worker_completes_it(
    company_and_platform, db_session, monkeypatch
):
    company, platform = company_and_platform
    db_session.add(CompanyAgent(company_id=company.id, agent_key="monitoring", status="active"))
    await db_session.flush()
    registry = build_default_agent_registry()
    service = AgentTaskService(db_session, registry)
    task = await service.create_task(
        company.id,
        requested_by="test-user",
        agent_key="monitoring",
        capability="platform.discover",
        title="Discover the public platform",
        input_payload={"platform_id": str(platform.id)},
        idempotency_key=f"monitoring-{uuid.uuid4()}",
        priority=50,
        max_attempts=2,
    )
    assert task.status == "queued"
    await db_session.commit()

    outbox = await db_session.scalar(
        select(AgentTaskOutbox).where(AgentTaskOutbox.task_id == task.id)
    )
    assert outbox is not None
    assert outbox.attempt_number == 1
    assert outbox.status == "pending"

    worker = AgentTaskWorker()

    class Dispatcher:
        async def dispatch(self, agent_key, capability, context):
            assert agent_key == "monitoring"
            assert capability == "platform.discover"
            return {"platform_id": str(platform.id), "scanned": 3}

    worker.dispatcher = Dispatcher()
    try:
        claimed_id = await worker._claim_one()
        assert claimed_id == outbox.id
        await worker._process(claimed_id)
    finally:
        await worker.engine.dispose()

    refreshed = await db_session.get(AgentTask, task.id)
    await db_session.refresh(refreshed)
    assert refreshed.status == "completed"
    assert refreshed.result_payload == {"platform_id": str(platform.id), "scanned": 3}
    await db_session.refresh(outbox)
    assert outbox.status == "delivered"


async def test_manual_retry_writes_next_outbox_attempt(db_session):
    from app.models import Company

    company = Company(name="Outbox Retry", slug=f"outbox-retry-{uuid.uuid4().hex[:8]}")
    db_session.add(company)
    await db_session.flush()
    db_session.add(CompanyAgent(company_id=company.id, agent_key="monitoring", status="active"))
    await db_session.flush()
    service = AgentTaskService(db_session, build_default_agent_registry())
    task = await service.create_task(
        company.id, requested_by="test-user", agent_key="monitoring",
        capability="platform.discover", title="Retryable task",
        input_payload={"platform_id": str(uuid.uuid4())}, idempotency_key=None,
        priority=50, max_attempts=3,
    )
    run = await service.start_run(company.id, task.id, worker_id="test-worker")
    await service.fail_run(
        company.id, task.id, run.id, error_code="temporary", error_message="temporary failure"
    )
    await service.retry_task(company.id, task.id, requested_by="test-user", delay_seconds=0)
    rows = list((await db_session.scalars(
        select(AgentTaskOutbox).where(AgentTaskOutbox.task_id == task.id).order_by(
            AgentTaskOutbox.attempt_number
        )
    )).all())
    assert [row.attempt_number for row in rows] == [1, 2]
    assert rows[1].status == "pending"
