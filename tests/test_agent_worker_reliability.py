"""PostgreSQL integration tests for multi-instance agent worker leasing."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.agents import build_default_agent_registry
from app.models import AgentTaskOutbox, CompanyAgent
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
        requested_by="worker-reliability-test",
        agent_key="monitoring",
        capability="platform.discover",
        title="Exercise PostgreSQL worker leasing",
        input_payload={"platform_id": str(uuid.uuid4())},
        idempotency_key=f"worker-lease-{uuid.uuid4()}",
        priority=50,
        max_attempts=3,
    )
    await db_session.commit()
    outbox = await db_session.scalar(
        select(AgentTaskOutbox).where(AgentTaskOutbox.task_id == task.id)
    )
    assert outbox is not None
    return outbox


async def test_two_agent_workers_cannot_claim_the_same_outbox_row(
    company_and_platform, db_session
):
    company, _ = company_and_platform
    outbox = await _create_dispatchable_task(db_session, company)
    first = AgentTaskWorker()
    second = AgentTaskWorker()
    try:
        claims = await asyncio.gather(first._claim_one(), second._claim_one())

        assert claims.count(outbox.id) == 1
        assert claims.count(None) == 1

        await db_session.refresh(outbox)
        assert outbox.status == "processing"
        assert outbox.worker_id in {first.worker_id, second.worker_id}
    finally:
        await asyncio.gather(first.engine.dispose(), second.engine.dispose())


async def test_agent_worker_reclaims_an_expired_outbox_lease(
    company_and_platform, db_session
):
    company, _ = company_and_platform
    outbox = await _create_dispatchable_task(db_session, company)
    outbox.status = "processing"
    outbox.worker_id = "agent-worker-crashed"
    outbox.locked_at = datetime.now(UTC) - timedelta(minutes=5)
    await db_session.commit()

    worker = AgentTaskWorker()
    worker.lease_seconds = 60
    try:
        claimed_id = await worker._claim_one()

        assert claimed_id == outbox.id
        await db_session.refresh(outbox)
        assert outbox.status == "processing"
        assert outbox.worker_id == worker.worker_id
        assert outbox.locked_at is not None
        assert outbox.locked_at > datetime.now(UTC) - timedelta(seconds=10)
    finally:
        await worker.engine.dispose()
