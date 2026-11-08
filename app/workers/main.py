"""Workflow worker process: claims queue jobs and drives the engine.

Run with ``python -m app.workers.main`` (docker-compose service ``worker``).

- configurable concurrency (``WORKER_CONCURRENCY``)
- reliable claim: main -> processing (atomically), acked on completion
- startup recovery: in-flight jobs from a dead worker are pushed back
- heartbeat per running execution; abandoned executions recovered by a poller
- graceful shutdown: stop claiming, finish the in-flight job, close resources
- worker-level job retry with backoff for transient failures (missing
  execution rows right after enqueue, transient DB loss)
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import uuid
from datetime import UTC, datetime

from app.api.deps import build_provider_executor
from app.core.config import get_settings
from app.core.logging import setup_logging
from app.core.queue import WorkflowJob, WorkflowQueue
from app.core.redis import RedisConnectionManager
from app.database.session import create_engine, create_session_factory
from app.integrations.providers import build_default_registry
from app.security.crypto import MasterKeyBox
from app.services.workflow_execution_service import WorkflowExecutionService
from app.workflows.engine import WorkflowEngine

logger = logging.getLogger("app.worker")

JOB_MAX_ATTEMPTS = 3
JOB_RETRY_DELAY_SECONDS = 2.0


class WorkflowWorker:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.worker_id = f"worker-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self.concurrency = int(os.getenv("WORKER_CONCURRENCY", "4"))
        self.poll_interval = float(os.getenv("WORKER_POLL_INTERVAL", "0.5"))
        self.recovery_interval = float(os.getenv("WORKER_RECOVERY_INTERVAL", "60"))

        self.engine_db = create_engine(self.settings)
        self.session_factory = create_session_factory(self.engine_db)
        self.redis_manager = RedisConnectionManager(
            self.settings.redis_url,
            socket_timeout=self.settings.redis_socket_timeout,
            socket_connect_timeout=self.settings.redis_socket_connect_timeout,
        )
        self.master_key_box = MasterKeyBox.from_settings(self.settings)
        self.registry = build_default_registry()
        self.executor = build_provider_executor(self.settings)
        self.queue: WorkflowQueue | None = None

        self._stopping = asyncio.Event()
        self._inflight: set[asyncio.Task] = set()

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        setup_logging(self.settings)
        logger.info("worker %s starting (concurrency=%d)", self.worker_id, self.concurrency)
        await self.redis_manager.initialize()
        assert self.redis_manager.client is not None
        self.queue = WorkflowQueue(self.redis_manager.client)

        await self.queue.requeue_processing()  # jobs left by a dead worker
        await self._recover_abandoned()

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._stopping.set)
            except NotImplementedError:  # pragma: no cover - Windows
                signal.signal(sig, lambda *_: self._stopping.set())

        scheduler_task = asyncio.create_task(self._scheduler_loop())
        recovery_task = asyncio.create_task(self._recovery_loop())
        consumers = [asyncio.create_task(self._consume_loop(i)) for i in range(self.concurrency)]

        await self._stopping.wait()
        logger.info("shutdown requested; draining in-flight jobs...")
        for task in consumers:
            task.cancel()
        await asyncio.gather(*consumers, return_exceptions=True)
        scheduler_task.cancel()
        recovery_task.cancel()
        await asyncio.gather(scheduler_task, recovery_task, return_exceptions=True)
        if self._inflight:
            await asyncio.gather(*self._inflight, return_exceptions=True)
        await self.redis_manager.close()
        await self.engine_db.dispose()
        logger.info("worker %s stopped cleanly", self.worker_id)

    def _stopping_set(self) -> None:  # pragma: no cover - signal path
        self._stopping.set()

    # -------------------------------------------------------------------- loops
    async def _consume_loop(self, index: int) -> None:
        while not self._stopping.is_set():
            try:
                job = await self.queue.claim()
                if job is None:
                    await asyncio.sleep(self.poll_interval)
                    continue
                task = asyncio.create_task(self._process_job(job))
                self._inflight.add(task)
                task.add_done_callback(self._inflight.discard)
            except asyncio.CancelledError:
                return
            except Exception:  # noqa: BLE001 - keep the consumer alive
                logger.exception("consumer loop %d error", index)
                await asyncio.sleep(1.0)

    async def _process_job(self, job: WorkflowJob) -> None:
        for attempt in range(1, JOB_MAX_ATTEMPTS + 1):
            try:
                async with self.session_factory() as session:
                    from app.models import WorkflowExecution  # noqa: PLC0415

                    engine = WorkflowEngine(session, self.registry, self.executor, self.master_key_box)

                    execution = await session.get(WorkflowExecution, job.execution_id)
                    if execution is None:
                        # Enqueue raced the API transaction; retry shortly.
                        if attempt < JOB_MAX_ATTEMPTS:
                            await asyncio.sleep(JOB_RETRY_DELAY_SECONDS)
                            continue
                        logger.error("job %s references missing execution; dropping", job.execution_id)
                        await self.queue.complete(job)
                        return
                    if execution.company_id != job.company_id:
                        logger.error("job %s tenant mismatch; dropping", job.execution_id)
                        await self.queue.complete(job)
                        return
                    if execution.status in ("completed", "failed", "cancelled", "timed_out", "succeeded"):
                        await self.queue.complete(job)
                        return

                    execution.worker_id = self.worker_id
                    execution.heartbeat_at = datetime.now(UTC)
                    await session.commit()

                    await engine.run_execution(job.execution_id, company_id=job.company_id)
                    await session.commit()
                await self.queue.complete(job)
                logger.info(
                    "job for execution %s processed (attempt %d)", job.execution_id, job.attempt
                )
                return
            except asyncio.CancelledError:
                # Graceful shutdown mid-job: leave it in `processing`; the next
                # worker start requeues it.
                raise
            except Exception:  # noqa: BLE001 - retry with backoff, then abandon
                logger.exception("job %s processing error (attempt %d)", job.execution_id, attempt)
                if attempt >= JOB_MAX_ATTEMPTS:
                    await self._fail_job(job)
                    await self.queue.complete(job)
                    return
                await asyncio.sleep(JOB_RETRY_DELAY_SECONDS * attempt)

    async def _fail_job(self, job: WorkflowJob) -> None:
        try:
            async with self.session_factory() as session:
                from app.models import WorkflowExecution  # noqa: PLC0415

                execution = await session.get(WorkflowExecution, job.execution_id)
                if execution is not None and execution.status not in (
                    "completed", "failed", "cancelled", "timed_out", "succeeded"
                ):
                    execution.status = "failed"
                    execution.error_message = "Worker failed to process the job after repeated attempts."
                    execution.completed_at = datetime.now(UTC)
                    await session.commit()
        except Exception:  # noqa: BLE001
            logger.exception("could not mark execution %s failed", job.execution_id)

    async def _recover_abandoned(self) -> None:
        async with self.session_factory() as session:
            service = WorkflowExecutionService(
                session, self.registry, self.executor, self.master_key_box, queue=self.queue
            )
            await service.recover_abandoned_executions(stale_after_seconds=120)
            await session.commit()

    async def _recovery_loop(self) -> None:
        while not self._stopping.is_set():
            await asyncio.sleep(self.recovery_interval)
            try:
                await self._recover_abandoned()
            except Exception:  # noqa: BLE001
                logger.exception("abandoned-execution recovery failed")

    async def _scheduler_loop(self) -> None:
        from app.services.workflow_schedule_service import WorkflowScheduleService  # noqa: PLC0415

        while not self._stopping.is_set():
            try:
                async with self.session_factory() as session:
                    service = WorkflowScheduleService(session, queue=self.queue)
                    created = await service.collect_due_schedules()
                    await session.commit()
                    if created:
                        logger.info("scheduler fired %d schedule(s)", len(created))
            except Exception:  # noqa: BLE001
                logger.exception("scheduler loop error")
            await asyncio.sleep(float(os.getenv("WORKER_SCHEDULER_INTERVAL", "5")))


def main() -> None:
    worker = WorkflowWorker()
    asyncio.run(worker.start())


if __name__ == "__main__":
    main()
