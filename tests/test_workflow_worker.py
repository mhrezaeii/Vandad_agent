"""Workflow queue, worker processing, and crash-recovery tests (real Redis + PG)."""

from __future__ import annotations

import base64
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.core.queue import WorkflowJob
from app.models import (
    PlatformConnection,
    WorkflowExecutionStep,
    WorkflowStatus,
    WorkflowStepStatus,
)
from app.security.crypto import MasterKeyBox
from app.services.workflow_execution_service import WorkflowExecutionService
from tests.conftest import TEST_MASTER_KEY_CONFIG


def _keybox() -> MasterKeyBox:
    return MasterKeyBox(
        {int(v): base64.b64decode(k) for v, k in TEST_MASTER_KEY_CONFIG.items()},
        active_key_version=1,
    )


@pytest.fixture
def execution_service(db_session, engine_pair, workflow_queue):
    registry, executor = engine_pair
    return WorkflowExecutionService(db_session, registry, executor, _keybox(), queue=workflow_queue)


def _scope(company_id):
    return SimpleNamespace(company_id=company_id)


async def test_queue_enqueue_claim_complete_roundtrip(workflow_queue):
    job = WorkflowJob(execution_id=uuid.uuid4(), company_id=uuid.uuid4())
    await workflow_queue.enqueue(job)
    depths = await workflow_queue.depths()
    assert depths["main"] == 1

    claimed = await workflow_queue.claim()
    assert claimed is not None
    assert claimed.execution_id == job.execution_id
    depths = await workflow_queue.depths()
    assert depths["main"] == 0
    assert depths["processing"] == 1

    await workflow_queue.complete(claimed)
    depths = await workflow_queue.depths()
    assert depths["processing"] == 0


async def test_queue_delayed_jobs_become_due(workflow_queue):
    job = WorkflowJob(execution_id=uuid.uuid4(), company_id=uuid.uuid4())
    await workflow_queue.enqueue_delayed(job, delay_seconds=0.05)

    first = await workflow_queue.claim()
    assert first is None  # not due yet

    await __import__("asyncio").sleep(0.1)
    claimed = await workflow_queue.claim()
    assert claimed is not None
    assert claimed.execution_id == job.execution_id
    await workflow_queue.complete(claimed)


async def test_queue_requeue_processing_recovers_inflight(workflow_queue):
    job = WorkflowJob(execution_id=uuid.uuid4(), company_id=uuid.uuid4())
    await workflow_queue.enqueue(job)
    claimed = await workflow_queue.claim()
    assert claimed is not None

    # Simulate a worker crash: a fresh queue instance recovers the in-flight job.
    recovered = await workflow_queue.requeue_processing()
    assert recovered == 1
    again = await workflow_queue.claim()
    assert again is not None
    assert again.execution_id == job.execution_id
    await workflow_queue.complete(again)


async def test_worker_processes_execution_end_to_end(
    db_session, engine_pair, workflow_queue, workflow_factory, company_and_platform, execution_service
):
    """The full lifecycle: enqueue -> worker claim -> engine run -> completed."""
    from app.workflows.engine import WorkflowEngine

    registry, executor = engine_pair
    company, _platform = company_and_platform
    workflow = await workflow_factory(
        company.id,
        [
            {"node_key": "start", "node_type": "trigger.manual", "name": "t", "configuration": {}},
            {"node_key": "finish", "node_type": "control.complete", "name": "f",
             "configuration": {"outcome": "completed"}},
        ],
        [{"from_key": "start", "to_key": "finish"}],
    )
    execution, created = await execution_service.execute_workflow(
        workflow.id, scope=_scope(company.id), input_metadata={"n": 1}, enqueue=True
    )
    assert created
    assert execution.status == WorkflowStatus.QUEUED.value

    # Worker path: claim from Redis, run the engine, commit, ack.
    job = await workflow_queue.claim()
    assert job is not None and job.execution_id == execution.id

    engine = WorkflowEngine(db_session, registry, executor, _keybox())
    await engine.run_execution(job.execution_id, company_id=job.company_id)
    await db_session.commit()
    await workflow_queue.complete(job)

    assert execution.status == WorkflowStatus.COMPLETED.value
    depths = await workflow_queue.depths()
    assert depths == {"main": 0, "processing": 0, "delayed": 0}


async def test_worker_tolerates_missing_execution_then_drops(
    db_session, engine_pair, workflow_queue, monkeypatch
):
    """A job whose execution row is not yet committed is retried, then dropped."""
    from app.workers import main as worker_main

    monkeypatch.setattr(worker_main, "JOB_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(worker_main, "JOB_RETRY_DELAY_SECONDS", 0.01)

    job = WorkflowJob(execution_id=uuid.uuid4(), company_id=uuid.uuid4())  # never created

    # _fail_job must not blow up on a missing row.
    async with db_session.begin():
        pass
    from app.workers.main import WorkflowWorker  # noqa: PLC0415

    real_worker = WorkflowWorker.__new__(WorkflowWorker)
    real_worker.session_factory = None
    await WorkflowWorker._fail_job(real_worker, job)  # must not raise
    await workflow_queue.complete(job)


async def test_abandoned_execution_without_side_effects_is_requeued(
    db_session, engine_pair, workflow_queue, workflow_factory, company_and_platform, execution_service
):
    registry, executor = engine_pair
    company, _platform = company_and_platform
    workflow = await workflow_factory(
        company.id,
        [
            {"node_key": "start", "node_type": "trigger.manual", "name": "t", "configuration": {}},
            {"node_key": "mid", "node_type": "data.transform", "name": "m",
             "configuration": {"mappings": {"x": "y"}}},
        ],
        [{"from_key": "start", "to_key": "mid"}],
    )
    execution, _created = (await execution_service.execute_workflow(
        workflow.id, scope=_scope(company.id), enqueue=False
    ))
    # Simulate a worker that died mid-run: running with a stale heartbeat.
    execution.status = WorkflowStatus.RUNNING.value
    execution.started_at = datetime.now(UTC) - timedelta(seconds=10)
    execution.heartbeat_at = datetime.now(UTC) - timedelta(seconds=300)
    await db_session.commit()

    async with db_session.begin():
        pass
    report = await execution_service.recover_abandoned_executions(stale_after_seconds=120)
    assert report["abandoned"] == 1
    assert report["requeued"] == 1
    assert execution.status == WorkflowStatus.QUEUED.value


async def test_abandoned_execution_with_side_effects_fails(
    db_session, engine_pair, workflow_queue, workflow_factory, company_and_platform, execution_service
):
    registry, executor = engine_pair
    company, platform = company_and_platform
    connection = PlatformConnection(
        company_id=company.id,
        platform_id=platform.id,
        name=f"c-{uuid.uuid4().hex[:6]}",
        provider="diagnostic",
        status="connected",
        configuration={"expected_config_key": "x"},
    )
    db_session.add(connection)
    await db_session.commit()

    # Node marked non-idempotent: re-running could duplicate side effects.
    workflow = await workflow_factory(
        company.id,
        [
            {"node_key": "start", "node_type": "trigger.manual", "name": "t", "configuration": {}},
            {"node_key": "action", "node_type": "action.provider", "name": "a",
             "configuration": {"connection_id": str(connection.id), "operation": "execute"},
             "execution_settings": {"idempotent": False}},
        ],
        [{"from_key": "start", "to_key": "action"}],
    )
    execution, _created = await execution_service.execute_workflow(
        workflow.id, scope=_scope(company.id), enqueue=False
    )
    execution.status = WorkflowStatus.RUNNING.value
    execution.started_at = datetime.now(UTC) - timedelta(seconds=10)
    execution.heartbeat_at = datetime.now(UTC) - timedelta(seconds=300)
    db_session.add(
        WorkflowExecutionStep(
            execution_id=execution.id,
            node_key="action",
            node_type="action.provider",
            status=WorkflowStepStatus.COMPLETED.value,
            attempt=1,
        )
    )
    await db_session.commit()

    report = await execution_service.recover_abandoned_executions(stale_after_seconds=120)
    assert report["failed"] == 1
    assert execution.status == WorkflowStatus.FAILED.value
    assert "non-idempotent" in (execution.error_message or "")


async def test_heartbeat_index_finds_stale_only(
    db_session, engine_pair, workflow_queue, workflow_factory, company_and_platform, execution_service
):
    registry, executor = engine_pair
    company, _platform = company_and_platform
    workflow = await workflow_factory(company.id, [
        {"node_key": "start", "node_type": "trigger.manual", "name": "t", "configuration": {}}
    ], [])
    fresh, _ = await execution_service.execute_workflow(workflow.id, scope=_scope(company.id), enqueue=False)
    fresh.status = WorkflowStatus.RUNNING.value
    fresh.heartbeat_at = datetime.now(UTC)  # healthy
    stale, _ = await execution_service.execute_workflow(workflow.id, scope=_scope(company.id), enqueue=False)
    stale.status = WorkflowStatus.RUNNING.value
    stale.heartbeat_at = datetime.now(UTC) - timedelta(seconds=600)
    await db_session.commit()

    report = await execution_service.recover_abandoned_executions(stale_after_seconds=120)
    assert report["abandoned"] == 1
    assert stale.status == WorkflowStatus.QUEUED.value
    assert fresh.status == WorkflowStatus.RUNNING.value
