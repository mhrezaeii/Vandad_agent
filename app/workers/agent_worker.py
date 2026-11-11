"""PostgreSQL-outbox worker for agent tasks."""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select

from app.agents import build_default_agent_registry
from app.agents.runtime import (
    AgentDispatcher,
    AgentExecutionContext,
    ContentAgentHandler,
    MonitoringAgentHandler,
)
from app.api.deps import build_provider_executor
from app.core.config import get_settings
from app.database.session import create_engine, create_session_factory
from app.integrations.providers import build_default_registry
from app.models import AgentTask, AgentTaskOutbox, AgentTaskRun
from app.security.crypto import MasterKeyBox
from app.services.agent_task_service import AgentTaskService

logger = logging.getLogger("app.agent_worker")


class AgentTaskWorker:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.worker_id = f"agent-worker-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self.concurrency = max(1, int(os.getenv("AGENT_WORKER_CONCURRENCY", "3")))
        self.poll_interval = max(0.1, float(os.getenv("AGENT_WORKER_POLL_INTERVAL", "1")))
        self.lease_seconds = max(60, int(os.getenv("AGENT_WORKER_LEASE_SECONDS", "120")))
        self.shutdown_timeout = max(1, int(os.getenv("AGENT_WORKER_SHUTDOWN_TIMEOUT_SECONDS", "30")))
        self.engine = create_engine(self.settings)
        self.session_factory = create_session_factory(self.engine)
        self.agent_registry = build_default_agent_registry()
        self.provider_registry = build_default_registry()
        self.provider_executor = build_provider_executor(self.settings)
        self.master_key_box = MasterKeyBox.from_settings(self.settings)
        self.dispatcher = AgentDispatcher(
            self.agent_registry, [MonitoringAgentHandler(), ContentAgentHandler()]
        )
        self.stopping = asyncio.Event()
        self.inflight: set[asyncio.Task] = set()
        self.slots = asyncio.Semaphore(self.concurrency)

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self.stopping.set)
            except NotImplementedError:
                signal.signal(sig, lambda *_: self.stopping.set())
        logger.info("agent worker %s started (concurrency=%d)", self.worker_id, self.concurrency)
        consumers = [asyncio.create_task(self._consume(index)) for index in range(self.concurrency)]
        recovery = asyncio.create_task(self._recover_stale_runs())
        await self.stopping.wait()
        for task in consumers:
            task.cancel()
        recovery.cancel()
        await asyncio.gather(*consumers, recovery, return_exceptions=True)
        if self.inflight:
            pending = set(self.inflight)
            try:
                await asyncio.wait_for(
                    asyncio.gather(*pending, return_exceptions=True),
                    timeout=self.shutdown_timeout,
                )
            except TimeoutError:
                logger.warning("agent worker %s drain timed out; cancelling %d deliveries",
                               self.worker_id, len(pending))
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
        await self.engine.dispose()
        logger.info("agent worker %s stopped", self.worker_id)

    async def _consume(self, index: int) -> None:
        while not self.stopping.is_set():
            await self.slots.acquire()
            try:
                outbox_id = await self._claim_one()
            except Exception:
                self.slots.release()
                logger.exception("agent worker consumer %d could not claim an outbox row", index)
                await asyncio.sleep(self.poll_interval)
                continue
            if outbox_id is None:
                self.slots.release()
                await asyncio.sleep(self.poll_interval)
                continue
            task = asyncio.create_task(self._process_with_release(outbox_id))
            self.inflight.add(task)
            task.add_done_callback(self.inflight.discard)

    async def _process_with_release(self, outbox_id: uuid.UUID) -> None:
        try:
            await self._process(outbox_id)
        finally:
            self.slots.release()

    async def _claim_one(self) -> uuid.UUID | None:
        now = datetime.now(UTC)
        stale_before = now - timedelta(seconds=self.lease_seconds)
        async with self.session_factory() as session:
            row = await session.scalar(
                select(AgentTaskOutbox)
                .where(
                    or_(
                        (AgentTaskOutbox.status == "pending") & (AgentTaskOutbox.available_at <= now),
                        (AgentTaskOutbox.status == "processing") & (AgentTaskOutbox.locked_at < stale_before),
                    )
                )
                .order_by(AgentTaskOutbox.available_at, AgentTaskOutbox.created_at)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if row is None:
                return None
            row.status = "processing"
            row.locked_at = now
            row.worker_id = self.worker_id
            row.delivery_attempts += 1
            await session.commit()
            return row.id

    async def _heartbeat(
        self, task_id: uuid.UUID, run_id: uuid.UUID, outbox_id: uuid.UUID,
        cancel_event: asyncio.Event,
    ) -> None:
        interval = max(5, min(15, self.lease_seconds // 3))
        while True:
            await asyncio.sleep(interval)
            now = datetime.now(UTC)
            async with self.session_factory() as session:
                run = await session.get(AgentTaskRun, run_id)
                outbox = await session.get(AgentTaskOutbox, outbox_id)
                task = await session.get(AgentTask, task_id)
                if (
                    run is None or run.status != "running" or run.worker_id != self.worker_id
                    or outbox is None or outbox.status != "processing"
                    or outbox.worker_id != self.worker_id
                ):
                    return
                if task is None or task.status != "running":
                    return
                if task.cancel_requested:
                    cancel_event.set()
                run.heartbeat_at = now
                outbox.locked_at = now
                await session.commit()

    async def _process(self, outbox_id: uuid.UUID) -> None:
        heartbeat_task = None
        task_id = company_id = run_id = None
        try:
            async with self.session_factory() as session:
                outbox = await session.get(AgentTaskOutbox, outbox_id)
                if outbox is None or outbox.status != "processing" or outbox.worker_id != self.worker_id:
                    return
                task_id, company_id = outbox.task_id, outbox.company_id
                task = await session.get(AgentTask, task_id)
                if task is None or task.company_id != company_id:
                    outbox.status = "delivered"
                    outbox.locked_at = None
                    await session.commit()
                    return
                if outbox.attempt_number != task.attempt_count + 1:
                    outbox.status = "delivered"
                    outbox.locked_at = None
                    await session.commit()
                    return
                service = AgentTaskService(session, self.agent_registry)
                try:
                    run = await service.start_run(company_id, task_id, worker_id=self.worker_id)
                except ConflictError:
                    if task.status in {"completed", "cancelled", "running"}:
                        outbox.status = "delivered"
                        outbox.locked_at = None
                        await session.commit()
                        return
                    raise
                run_id = run.id
                agent_key, capability = task.agent_key, task.capability
                payload = dict(task.input_payload)
                await session.commit()

            cancel_event = asyncio.Event()
            heartbeat_task = asyncio.create_task(
                self._heartbeat(task_id, run_id, outbox_id, cancel_event)
            )
            context = AgentExecutionContext(
                company_id=company_id, task_id=task_id, run_id=run_id,
                worker_id=self.worker_id, payload=payload,
                session_factory=self.session_factory,
                provider_registry=self.provider_registry,
                provider_executor=self.provider_executor,
                master_key_box=self.master_key_box,
                cancel_event=cancel_event,
            )
            result = await self.dispatcher.dispatch(agent_key, capability, context)
            context.raise_if_cancelled()

            async with self.session_factory() as session:
                service = AgentTaskService(session, self.agent_registry)
                await service.complete_run(
                    company_id, task_id, run_id, result_payload=result, worker_id=self.worker_id
                )
                outbox = await session.get(AgentTaskOutbox, outbox_id)
                if outbox is not None and outbox.worker_id == self.worker_id:
                    outbox.status = "delivered"
                    outbox.locked_at = None
                    outbox.last_error = None
                await session.commit()
        except asyncio.CancelledError:
            # Leave the lease to expire; recovery fences this worker after process exit.
            raise
        except Exception as exc:
            from app.security.redaction import redact_text

            safe_message = redact_text(str(exc))[:1000]
            safe_code = str(getattr(exc, "code", "agent_execution_failed"))[:100]
            logger.exception("agent task delivery %s failed", outbox_id)
            if task_id is not None and company_id is not None and run_id is not None:
                async with self.session_factory() as session:
                    service = AgentTaskService(session, self.agent_registry)
                    try:
                        task = await service.fail_run(
                            company_id, task_id, run_id,
                            error_code=safe_code, error_message=safe_message,
                            worker_id=self.worker_id,
                        )
                        if task.status == "failed" and task.attempt_count < task.max_attempts:
                            await service.retry_task(
                                company_id, task_id, requested_by=self.worker_id,
                                delay_seconds=min(2 ** max(task.attempt_count - 1, 0), 60),
                            )
                        outbox = await session.get(AgentTaskOutbox, outbox_id)
                        if outbox is not None and outbox.worker_id == self.worker_id:
                            outbox.status = "delivered"
                            outbox.locked_at = None
                            outbox.last_error = safe_message
                        await session.commit()
                    except ConflictError:
                        await session.rollback()
                    except Exception:
                        await session.rollback()
                        logger.exception("could not persist failed agent run %s", run_id)
            else:
                async with self.session_factory() as session:
                    outbox = await session.get(AgentTaskOutbox, outbox_id)
                    if outbox is not None and outbox.worker_id == self.worker_id:
                        outbox.status = "pending"
                        outbox.locked_at = None
                        outbox.available_at = datetime.now(UTC) + timedelta(seconds=5)
                        outbox.last_error = safe_message
                    await session.commit()
        finally:
            if heartbeat_task is not None:
                heartbeat_task.cancel()
                await asyncio.gather(heartbeat_task, return_exceptions=True)

    async def _recover_stale_runs(self) -> None:
        while not self.stopping.is_set():
            await asyncio.sleep(30)
            cutoff = datetime.now(UTC) - timedelta(seconds=self.lease_seconds * 2)
            async with self.session_factory() as session:
                stale = list((await session.scalars(
                    select(AgentTaskRun).where(
                        AgentTaskRun.status == "running",
                        or_(AgentTaskRun.heartbeat_at < cutoff, AgentTaskRun.heartbeat_at.is_(None)),
                    ).order_by(AgentTaskRun.heartbeat_at).limit(50)
                )).all())
                for run in stale:
                    service = AgentTaskService(session, self.agent_registry)
                    try:
                        task = await service.fail_run(
                            run.company_id, run.task_id, run.id,
                            error_code="worker_lease_expired",
                            error_message="Agent worker lease expired; execution was recovered.",
                            heartbeat_before=cutoff,
                        )
                        if task.status == "failed" and task.attempt_count < task.max_attempts:
                            await service.retry_task(
                                task.company_id, task.id, requested_by=self.worker_id, delay_seconds=1
                            )
                        await session.commit()
                    except ConflictError:
                        await session.rollback()
                    except Exception:
                        await session.rollback()
                        logger.exception("stale run recovery failed for %s", run.id)
