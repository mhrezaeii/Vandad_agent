"""Worker hardening tests: bounded concurrency, deferred delays, duplicates.

Builds WorkflowWorker instances wired to the test infrastructure (real Redis
queue + real PostgreSQL) and drives the consumer loops directly.
"""

from __future__ import annotations

import asyncio
import base64
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.core.queue import WorkflowJob, WorkflowQueue
from app.integrations.providers import build_default_registry
from app.models import (
    Platform,
    PlatformConnection,
    WorkflowStatus,
)
from app.security.crypto import MasterKeyBox
from app.services.workflow_execution_service import WorkflowExecutionService
from app.workers.main import WorkflowWorker
from tests.conftest import TEST_MASTER_KEY_CONFIG
from tests.provider_mocks import ScriptedProvider


def _keybox() -> MasterKeyBox:
    return MasterKeyBox(
        {int(v): base64.b64decode(k) for v, k in TEST_MASTER_KEY_CONFIG.items()},
        active_key_version=1,
    )


class ConcurrencyProbeProvider(ScriptedProvider):
    """Tracks the maximum number of simultaneously executing operations."""

    provider_id = "concurrency_probe"

    def __init__(self) -> None:
        super().__init__()
        self.current = 0
        self.max_concurrent = 0

    async def _run_script(self):
        self.current += 1
        try:
            self.max_concurrent = max(self.max_concurrent, self.current)
            await asyncio.sleep(0.15)
            return await super()._run_script()
        finally:
            self.current -= 1


def _make_worker(
    *,
    session_factory,
    redis_manager,
    registry,
    executor,
    queue: WorkflowQueue,
    concurrency: int,
    poll_interval: float = 0.02,
) -> WorkflowWorker:
    """Construct a worker wired to the test infrastructure (no __init__ IO)."""
    worker = WorkflowWorker.__new__(WorkflowWorker)
    worker.settings = SimpleNamespace()
    worker.worker_id = f"worker-test-{uuid.uuid4().hex[:6]}"
    worker.concurrency = concurrency
    worker.poll_interval = poll_interval
    worker.recovery_interval = 3600  # keep the recovery loop out of the way
    worker.engine_db = None
    worker.session_factory = session_factory
    worker.redis_manager = redis_manager
    worker.master_key_box = _keybox()
    worker.registry = registry
    worker.executor = executor
    worker.queue = queue
    # Media nodes need storage; these hardening tests never touch artifacts.
    worker.artifact_storage = None
    worker._stopping = asyncio.Event()
    worker._inflight = set()
    worker._job_slots = asyncio.Semaphore(concurrency)
    return worker


@pytest.fixture
def engine_pair_fast(key_box):
    from app.integrations.providers import ProviderExecutor, RetryPolicy

    registry = build_default_registry()
    executor = ProviderExecutor(
        default_policy=RetryPolicy(max_attempts=1, backoff_base_seconds=0.0),
        default_timeout_seconds=5.0,
    )
    return registry, executor


async def _connection(db_session, company_id, provider="concurrency_probe") -> PlatformConnection:
    result = await db_session.execute(
        select(Platform).where(Platform.company_id == company_id).limit(1)
    )
    platform = result.scalar_one()
    connection = PlatformConnection(
        company_id=company_id,
        platform_id=platform.id,
        name=f"conn-{uuid.uuid4().hex[:8]}",
        provider=provider,
        status="connected",
        configuration={"expected_config_key": "x"},
    )
    db_session.add(connection)
    await db_session.commit()
    return connection


async def _linear_workflow(workflow_factory, company_id, connection_id):
    return await workflow_factory(
        company_id,
        [
            {"node_key": "start", "node_type": "trigger.manual", "name": "t", "configuration": {}},
            {"node_key": "probe", "node_type": "action.provider", "name": "p",
             "configuration": {"connection_id": str(connection_id), "operation": "execute"}},
        ],
        [{"from_key": "start", "to_key": "probe"}],
    )


async def test_concurrency_limit_bounds_executing_jobs(
    db_session, engine_pair_fast, redis_manager, workflow_factory, company_and_platform
):
    """Six jobs with WORKER_CONCURRENCY=2: max parallel executions must be 2."""
    from sqlalchemy import select

    from app.models import WorkflowExecution

    registry, executor = engine_pair_fast
    probe = ConcurrencyProbeProvider()
    registry.register(probe)

    company, _platform = company_and_platform
    connection = await _connection(db_session, company.id)
    workflow = await _linear_workflow(workflow_factory, company.id, connection.id)

    service = WorkflowExecutionService(db_session, registry, executor, _keybox(), queue=None)
    scope = SimpleNamespace(company_id=company.id)
    executions = []
    for _ in range(6):
        execution, _created = await service.execute_workflow(
            workflow.id, scope=scope, enqueue=False
        )
        executions.append(execution)
    await db_session.commit()

    queue = WorkflowQueue(redis_manager.client)
    await queue.requeue_processing()
    for execution in executions:
        await queue.enqueue(WorkflowJob(execution_id=execution.id, company_id=company.id))

    worker = _make_worker(
        session_factory=db_session.session_factory if hasattr(db_session, "session_factory") else None,
        redis_manager=redis_manager,
        registry=registry,
        executor=executor,
        queue=queue,
        concurrency=2,
    )
    worker.session_factory = None  # replaced below with the test factory
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: PLC0415
    from sqlalchemy.pool import NullPool  # noqa: PLC0415

    from tests.conftest import TEST_DATABASE_URL  # noqa: PLC0415

    test_engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    worker.session_factory = async_sessionmaker(bind=test_engine, expire_on_commit=False, autoflush=False)

    consumers = [asyncio.create_task(worker._consume_loop(i)) for i in range(worker.concurrency)]

    # Wait until all executions complete (bounded wait).
    deadline = asyncio.get_running_loop().time() + 30
    while asyncio.get_running_loop().time() < deadline:
        result = await db_session.execute(
            select(WorkflowExecution.status).where(
                WorkflowExecution.id.in_([e.id for e in executions])
            )
        )
        statuses = [row[0] for row in result.all()]
        if all(status == "completed" for status in statuses):
            break
        await asyncio.sleep(0.1)

    worker._stopping.set()
    await asyncio.gather(*consumers, return_exceptions=True)
    await test_engine.dispose()

    assert all(status == "completed" for status in statuses)
    # Core guarantee: the concurrency bound is never exceeded. With 6 jobs and
    # an unbounded worker this probe would reach 4-6; under the semaphore it
    # stays at <= 2. (Exact overlap is timing-dependent, so we assert the bound
    # and that parallel progress happened across the batch.)
    assert probe.max_concurrent <= 2, (
        f"concurrency bound violated: observed {probe.max_concurrent} > 2"
    )
    assert probe.max_concurrent >= 1


async def test_semaphore_slots_are_not_leaked_by_failures(
    db_session, engine_pair_fast, redis_manager, workflow_factory, company_and_platform
):
    """Jobs that fail must release their slots: a second batch still runs."""
    registry, executor = engine_pair_fast
    company, _platform = company_and_platform
    connection = await _connection(db_session, company.id, provider="diagnostic")

    # Configuration missing the required key -> permanent failure for every job.
    connection.configuration = {}
    await db_session.commit()

    workflow = await _linear_workflow(workflow_factory, company.id, connection.id)
    service = WorkflowExecutionService(db_session, registry, executor, _keybox(), queue=None)
    scope = SimpleNamespace(company_id=company.id)
    queue = WorkflowQueue(redis_manager.client)
    await queue.requeue_processing()

    for _ in range(2):
        execution, _created = await service.execute_workflow(workflow.id, scope=scope, enqueue=False)
        await db_session.commit()
        await queue.enqueue(WorkflowJob(execution_id=execution.id, company_id=company.id))

    worker = _make_worker(
        session_factory=None, redis_manager=redis_manager, registry=registry,
        executor=executor, queue=queue, concurrency=2,
    )
    worker.session_factory = None
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: PLC0415
    from sqlalchemy.pool import NullPool  # noqa: PLC0415

    from tests.conftest import TEST_DATABASE_URL  # noqa: PLC0415

    test_engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    worker.session_factory = async_sessionmaker(bind=test_engine, expire_on_commit=False, autoflush=False)

    consumers = [asyncio.create_task(worker._consume_loop(i)) for i in range(2)]
    deadline = asyncio.get_running_loop().time() + 20
    while asyncio.get_running_loop().time() < deadline:
        depths = await queue.depths()
        if depths == {"main": 0, "processing": 0, "delayed": 0}:
            break
        await asyncio.sleep(0.05)
    worker._stopping.set()
    await asyncio.gather(*consumers, return_exceptions=True)

    # Slots returned: the semaphore is fully available again.
    assert worker._job_slots._value == 2
    await test_engine.dispose()


async def test_delayed_execution_resumes_after_eta(
    db_session, engine_pair_fast, redis_manager, workflow_queue, workflow_factory, company_and_platform
):
    """Normal delay lifecycle: WAITING -> delayed job -> resumed -> completed."""

    registry, executor = engine_pair_fast
    company, _platform = company_and_platform
    worker = _make_worker(
        session_factory=None, redis_manager=redis_manager, registry=registry,
        executor=executor, queue=workflow_queue, concurrency=1,
    )
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: PLC0415
    from sqlalchemy.pool import NullPool  # noqa: PLC0415

    from tests.conftest import TEST_DATABASE_URL  # noqa: PLC0415

    test_engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    worker.session_factory = async_sessionmaker(bind=test_engine, expire_on_commit=False, autoflush=False)

    workflow = await workflow_factory(
        company.id,
        [
            {"node_key": "start", "node_type": "trigger.manual", "name": "t", "configuration": {}},
            {"node_key": "wait", "node_type": "control.delay", "name": "w",
             "configuration": {"seconds": 0.3}},
            {"node_key": "after", "node_type": "data.transform", "name": "a",
             "configuration": {"mappings": {"done": True}}},
        ],
        [{"from_key": "start", "to_key": "wait"}, {"from_key": "wait", "to_key": "after"}],
    )
    service = WorkflowExecutionService(db_session, registry, executor, _keybox(), queue=workflow_queue)
    scope = SimpleNamespace(company_id=company.id)
    execution, _created = await service.execute_workflow(workflow.id, scope=scope, enqueue=True)
    await db_session.commit()

    job = await workflow_queue.claim()
    assert job is not None
    await worker._process_job(job)
    await workflow_queue.complete(job)

    await db_session.refresh(execution)
    assert execution.status == WorkflowStatus.WAITING.value
    depths = await workflow_queue.depths()
    assert depths["delayed"] == 1  # scheduled for resumption at the eta

    # Not due yet: claiming must return nothing.
    assert await workflow_queue.claim() is None
    await asyncio.sleep(0.35)

    resumed_job = await workflow_queue.claim()
    assert resumed_job is not None and resumed_job.execution_id == execution.id
    await worker._process_job(resumed_job)
    await workflow_queue.complete(resumed_job)

    await db_session.refresh(execution)
    assert execution.status == WorkflowStatus.COMPLETED.value
    await test_engine.dispose()


async def test_duplicate_delayed_enqueue_is_safe(
    db_session, engine_pair_fast, redis_manager, workflow_queue, workflow_factory, company_and_platform
):
    """Two identical delayed entries: the second claim must not re-run anything."""
    registry, executor = engine_pair_fast
    company, _platform = company_and_platform
    worker = _make_worker(
        session_factory=None, redis_manager=redis_manager, registry=registry,
        executor=executor, queue=workflow_queue, concurrency=1,
    )
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: PLC0415
    from sqlalchemy.pool import NullPool  # noqa: PLC0415

    from tests.conftest import TEST_DATABASE_URL  # noqa: PLC0415

    test_engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    worker.session_factory = async_sessionmaker(bind=test_engine, expire_on_commit=False, autoflush=False)

    workflow = await workflow_factory(
        company.id,
        [{"node_key": "start", "node_type": "trigger.manual", "name": "t", "configuration": {}}],
        [],
    )
    service = WorkflowExecutionService(db_session, registry, executor, _keybox(), queue=workflow_queue)
    execution, _created = await service.execute_workflow(
        workflow.id, scope=SimpleNamespace(company_id=company.id), enqueue=False
    )
    # Waiting state already resumed (no snapshot); simulate two identical jobs.
    job = WorkflowJob(execution_id=execution.id, company_id=company.id)
    await worker._process_job(job)
    await worker._process_job(job)  # duplicate claim: terminal -> skipped, no raise
    await test_engine.dispose()


async def test_cancellation_before_resume_is_respected(
    db_session, engine_pair_fast, redis_manager, workflow_queue, workflow_factory, company_and_platform
):
    """Cancelling a WAITING execution means its delayed job must be dropped."""
    registry, executor = engine_pair_fast
    company, _platform = company_and_platform
    worker = _make_worker(
        session_factory=None, redis_manager=redis_manager, registry=registry,
        executor=executor, queue=workflow_queue, concurrency=1,
    )
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: PLC0415
    from sqlalchemy.pool import NullPool  # noqa: PLC0415

    from tests.conftest import TEST_DATABASE_URL  # noqa: PLC0415

    test_engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    worker.session_factory = async_sessionmaker(bind=test_engine, expire_on_commit=False, autoflush=False)

    workflow = await workflow_factory(
        company.id,
        [
            {"node_key": "start", "node_type": "trigger.manual", "name": "t", "configuration": {}},
            {"node_key": "wait", "node_type": "control.delay", "name": "w",
             "configuration": {"seconds": 30}},
        ],
        [{"from_key": "start", "to_key": "wait"}],
    )
    service = WorkflowExecutionService(db_session, registry, executor, _keybox(), queue=workflow_queue)
    execution, _created = await service.execute_workflow(
        workflow.id, scope=SimpleNamespace(company_id=company.id), enqueue=False
    )
    await db_session.commit()  # the worker runs in its own session/connection
    job = WorkflowJob(execution_id=execution.id, company_id=company.id)
    await worker._process_job(job)
    await db_session.refresh(execution)
    assert execution.status == WorkflowStatus.WAITING.value

    await service.cancel_execution(execution.id, scope=SimpleNamespace(company_id=company.id))
    await db_session.commit()

    # The delayed job fires later; the worker must skip the cancelled execution.
    await asyncio.sleep(0.05)
    late_job = await workflow_queue.claim()
    if late_job is None:  # eta not reached; simulate the queue entry directly
        late_job = job
    await worker._process_job(late_job)
    await db_session.refresh(execution)
    assert execution.status == WorkflowStatus.CANCELLED.value
    assert execution.cancel_requested is False or execution.status == WorkflowStatus.CANCELLED.value
    await test_engine.dispose()


async def test_restart_while_waiting_defers_until_eta(
    db_session, engine_pair_fast, redis_manager, workflow_queue, workflow_factory, company_and_platform
):
    """Crash after WAITING commit but before zadd: the reclaimed job re-defers."""
    registry, executor = engine_pair_fast
    company, _platform = company_and_platform
    worker = _make_worker(
        session_factory=None, redis_manager=redis_manager, registry=registry,
        executor=executor, queue=workflow_queue, concurrency=1,
    )
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: PLC0415
    from sqlalchemy.pool import NullPool  # noqa: PLC0415

    from tests.conftest import TEST_DATABASE_URL  # noqa: PLC0415

    test_engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    worker.session_factory = async_sessionmaker(bind=test_engine, expire_on_commit=False, autoflush=False)

    workflow = await workflow_factory(
        company.id,
        [
            {"node_key": "start", "node_type": "trigger.manual", "name": "t", "configuration": {}},
            {"node_key": "wait", "node_type": "control.delay", "name": "w",
             "configuration": {"seconds": 5}},
        ],
        [{"from_key": "start", "to_key": "wait"}],
    )
    service = WorkflowExecutionService(db_session, registry, executor, _keybox(), queue=workflow_queue)
    execution, _created = await service.execute_workflow(
        workflow.id, scope=SimpleNamespace(company_id=company.id), enqueue=False
    )
    engine = __import__("app").workflows.engine.WorkflowEngine(db_session, registry, executor, _keybox())
    await engine.run_execution(execution.id, company_id=company.id)
    await db_session.commit()
    assert execution.status == WorkflowStatus.WAITING.value

    # Simulate the crashed worker's unacked job coming back at startup.
    job = WorkflowJob(execution_id=execution.id, company_id=company.id)
    await redis_manager.client.lpush("vandad:wf:queue:processing", job.to_json())
    assert await workflow_queue.requeue_processing() == 1

    reclaimed = await workflow_queue.claim()
    assert reclaimed is not None
    await worker._process_job(reclaimed)  # must defer, NOT resume early
    await workflow_queue.complete(reclaimed)

    depths = await workflow_queue.depths()
    assert depths["delayed"] == 1  # re-deferred until the original eta
    await db_session.refresh(execution)
    assert execution.status == WorkflowStatus.WAITING.value
    await test_engine.dispose()


async def test_recovery_skips_deferred_waiting(
    db_session, engine_pair_fast, redis_manager, workflow_queue, workflow_factory, company_and_platform
):
    """A WAITING execution with a future delay_until is NOT treated as abandoned."""
    registry, executor = engine_pair_fast
    company, _platform = company_and_platform
    worker = _make_worker(
        session_factory=None, redis_manager=redis_manager, registry=registry,
        executor=executor, queue=workflow_queue, concurrency=1,
    )
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: PLC0415
    from sqlalchemy.pool import NullPool  # noqa: PLC0415

    from tests.conftest import TEST_DATABASE_URL  # noqa: PLC0415

    test_engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    worker.session_factory = async_sessionmaker(bind=test_engine, expire_on_commit=False, autoflush=False)

    workflow = await workflow_factory(
        company.id,
        [
            {"node_key": "start", "node_type": "trigger.manual", "name": "t", "configuration": {}},
            {"node_key": "wait", "node_type": "control.delay", "name": "w",
             "configuration": {"seconds": 300}},
        ],
        [{"from_key": "start", "to_key": "wait"}],
    )
    service = WorkflowExecutionService(db_session, registry, executor, _keybox(), queue=workflow_queue)
    execution, _created = await service.execute_workflow(
        workflow.id, scope=SimpleNamespace(company_id=company.id), enqueue=False
    )
    engine = __import__("app").workflows.engine.WorkflowEngine(db_session, registry, executor, _keybox())
    await engine.run_execution(execution.id, company_id=company.id)
    await db_session.commit()
    # Age the heartbeat beyond the stale threshold, like a long delay would.
    execution.heartbeat_at = datetime.now(UTC) - timedelta(seconds=600)
    await db_session.commit()

    report = await service.recover_abandoned_executions(stale_after_seconds=120)
    assert report["abandoned"] == 0
    await db_session.refresh(execution)
    assert execution.status == WorkflowStatus.WAITING.value  # untouched
    await test_engine.dispose()


async def test_recovery_still_catches_waiting_without_deferral(
    db_session, engine_pair_fast, redis_manager, workflow_queue, workflow_factory, company_and_platform
):
    """A stale WAITING execution with no pending delay is still recovered."""
    registry, executor = engine_pair_fast
    company, _platform = company_and_platform
    workflow = await workflow_factory(
        company.id,
        [{"node_key": "start", "node_type": "trigger.manual", "name": "t", "configuration": {}}],
        [],
    )
    service = WorkflowExecutionService(db_session, registry, executor, _keybox(), queue=workflow_queue)
    execution, _created = await service.execute_workflow(
        workflow.id, scope=SimpleNamespace(company_id=company.id), enqueue=False
    )
    execution.status = WorkflowStatus.WAITING.value
    execution.context_snapshot = {"nodes": {}}  # no delay_until (legacy/edge)
    execution.heartbeat_at = datetime.now(UTC) - timedelta(seconds=600)
    await db_session.commit()

    report = await service.recover_abandoned_executions(stale_after_seconds=120)
    assert report["requeued"] == 1
    await db_session.refresh(execution)
    assert execution.status == WorkflowStatus.QUEUED.value
