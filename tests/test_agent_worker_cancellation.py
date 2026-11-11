"""Failure-injection coverage for cooperative agent-task cancellation."""

from __future__ import annotations

import uuid

from sqlalchemy import select

from app.agents import build_default_agent_registry
from app.models import AgentTask, AgentTaskOutbox, AgentTaskRun, CompanyAgent
from app.services.agent_task_service import AgentTaskService
from app.workers.agent_worker import AgentTaskWorker


async def _create_dispatchable_task(db_session, company):
    db_session.add(
        CompanyAgent(company_id=company.id, agent_key="monitoring", status="active")
    )
    await db_session.flush()
    service = AgentTaskService(db_session, build_default_agent_registry())
    task = await service.create_task(
        company.id,
        requested_by="worker-cancellation-test",
        agent_key="monitoring",
        capability="platform.discover",
        title="Exercise cooperative cancellation",
        input_payload={"platform_id": str(uuid.uuid4())},
        idempotency_key=f"worker-cancel-{uuid.uuid4()}",
        priority=50,
        max_attempts=3,
    )
    await db_session.commit()
    outbox = await db_session.scalar(
        select(AgentTaskOutbox).where(AgentTaskOutbox.task_id == task.id)
    )
    assert outbox is not None
    return task, outbox


async def test_worker_persists_cancellation_observed_at_handler_boundary(
    company_and_platform, db_session
):
    company, _ = company_and_platform
    task, outbox = await _create_dispatchable_task(db_session, company)
    worker = AgentTaskWorker()

    class CancellingDispatcher:
        async def dispatch(self, agent_key, capability, context):
            # Simulate a cancellation request arriving while the handler is running.
            async with context.session_factory() as session:
                persisted_task = await session.get(AgentTask, context.task_id)
                persisted_task.cancel_requested = True
                await session.commit()
            context.cancel_event.set()
            return {"must_not_be_persisted": True}

    worker.dispatcher = CancellingDispatcher()
    try:
        claimed_id = await worker._claim_one()
        assert claimed_id == outbox.id

        await worker._process(claimed_id)

        await db_session.refresh(task)
        await db_session.refresh(outbox)
        run = await db_session.scalar(
            select(AgentTaskRun).where(AgentTaskRun.task_id == task.id)
        )

        assert task.status == "cancelled"
        assert task.cancel_requested is True
        assert task.result_payload is None
        assert run is not None
        assert run.status == "cancelled"
        assert run.result_payload is None
        assert outbox.status == "delivered"
        assert outbox.worker_id == worker.worker_id
    finally:
        await worker.engine.dispose()
