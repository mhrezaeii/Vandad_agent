"""Workflow worker process: claims queue jobs and drives the engine.

Run with ``python -m app.workers.main`` (docker-compose service ``worker``).

- bounded concurrency: ``WORKER_CONCURRENCY`` limits the number of *executing*
  jobs via a semaphore acquired before each claim
- reliable claim: main -> processing (atomically), acked on completion
- startup recovery: in-flight jobs from a dead worker are pushed back
- deferred waiting executions are re-enqueued through the delayed queue
- heartbeat per running execution; abandoned executions recovered by a poller
- graceful shutdown: stop claiming, finish in-flight jobs, close resources
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
from app.integrations.video.factory import build_video_registry_from_settings
from app.security.crypto import MasterKeyBox
from app.services.workflow_execution_service import WorkflowExecutionService
from app.workflows.engine import WorkflowEngine

logger = logging.getLogger("app.worker")

JOB_MAX_ATTEMPTS = 3
JOB_RETRY_DELAY_SECONDS = 2.0

# Statuses a claimed job may run in; anything else is skipped gracefully.
RUNNABLE_EXECUTION_STATUSES = frozenset({"pending", "queued", "waiting"})
TERMINAL_EXECUTION_STATUSES = frozenset(
    {"completed", "failed", "cancelled", "timed_out", "succeeded"}
)


class WorkflowWorker:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.worker_id = f"worker-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self.concurrency = max(1, int(os.getenv("WORKER_CONCURRENCY", "4")))
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
        self.scene_video_registry = build_video_registry_from_settings(self.settings)
        self.queue: WorkflowQueue | None = None

        # Artifact storage for media-producing workflow nodes (same volume as
        # the API container so artifacts are shared across services).
        from app.storage.local import LocalArtifactStorage  # noqa: PLC0415

        self.artifact_storage = LocalArtifactStorage(
            self.settings.artifact_storage_root,
            max_size_bytes=self.settings.artifact_max_size_bytes,
        )

        self._stopping = asyncio.Event()
        self._inflight: set[asyncio.Task] = set()
        # Bounds *executing* jobs, not merely consumer loops: a slot is held
        # from just before a claim until the job finishes (or fails/cancels).
        self._job_slots = asyncio.Semaphore(self.concurrency)

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
        monitoring_task = asyncio.create_task(self._monitoring_loop())
        consumers = [asyncio.create_task(self._consume_loop(i)) for i in range(self.concurrency)]

        await self._stopping.wait()
        logger.info("shutdown requested; draining in-flight jobs...")
        for task in consumers:
            task.cancel()
        await asyncio.gather(*consumers, return_exceptions=True)
        scheduler_task.cancel()
        recovery_task.cancel()
        monitoring_task.cancel()
        await asyncio.gather(scheduler_task, recovery_task, monitoring_task, return_exceptions=True)
        if self._inflight:
            await asyncio.gather(*self._inflight, return_exceptions=True)
        await self.redis_manager.close()
        await self.engine_db.dispose()
        logger.info("worker %s stopped cleanly", self.worker_id)

    # -------------------------------------------------------------------- loops
    async def _consume_loop(self, index: int) -> None:
        """Claim jobs only while a concurrency slot is available.

        The slot is acquired *before* claiming so the worker never takes more
        jobs from the queue than it can execute simultaneously, and it is
        released by the job task itself (or here on every early-exit path).
        """
        while not self._stopping.is_set():
            await self._job_slots.acquire()
            if self._stopping.is_set():
                self._job_slots.release()
                return
            try:
                job = await self.queue.claim()
            except asyncio.CancelledError:
                self._job_slots.release()
                raise
            except Exception:  # noqa: BLE001 - keep the consumer alive
                self._job_slots.release()
                logger.exception("consumer loop %d claim error", index)
                await asyncio.sleep(1.0)
                continue
            if job is None:
                self._job_slots.release()
                await asyncio.sleep(self.poll_interval)
                continue
            task = asyncio.create_task(self._process_job_with_release(job))
            self._inflight.add(task)
            task.add_done_callback(self._inflight.discard)

    async def _process_job_with_release(self, job: WorkflowJob) -> None:
        """Run one job and always return its concurrency slot."""
        try:
            await self._process_job(job)
        finally:
            self._job_slots.release()

    async def _process_job(self, job: WorkflowJob) -> None:
        for attempt in range(1, JOB_MAX_ATTEMPTS + 1):
            try:
                async with self.session_factory() as session:
                    from app.models import WorkflowExecution  # noqa: PLC0415

                    engine = WorkflowEngine(
                        session,
                        self.registry,
                        self.executor,
                        self.master_key_box,
                        artifact_storage=self.artifact_storage,
                        scene_video_registry=self.scene_video_registry,
                    )

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
                    if execution.status in TERMINAL_EXECUTION_STATUSES:
                        await self.queue.complete(job)
                        return
                    if execution.status == "running":
                        # Another worker holds this execution (duplicate claim);
                        # never run it twice.
                        logger.warning(
                            "execution %s already running elsewhere; skipping duplicate claim",
                            job.execution_id,
                        )
                        await self.queue.complete(job)
                        return
                    if execution.status == "waiting":
                        # A scheduled delay may not have elapsed yet (e.g. the
                        # job was reclaimed after a crash): defer again until
                        # the snapshot's delay_until instead of resuming early.
                        if await self._defer_waiting_execution(execution, job):
                            await session.commit()
                            await self.queue.complete(job)
                            return

                    execution.worker_id = self.worker_id
                    execution.heartbeat_at = datetime.now(UTC)
                    await session.commit()

                    result = await engine.run_execution(job.execution_id, company_id=job.company_id)
                    await session.commit()

                    if result.outcome == "waiting":
                        # The engine parked on a delay node: schedule resumption
                        # through the delayed queue (crash-safe: if this worker
                        # dies before the zadd, the job stays in `processing`
                        # and the next start defers it again via delay_until).
                        await self._schedule_resumption(execution, job)
                await self.queue.complete(job)
                logger.info(
                    "job for execution %s processed (attempt %d, outcome %s)",
                    job.execution_id, job.attempt, result.outcome,
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

    async def _defer_waiting_execution(self, execution, job: WorkflowJob) -> bool:  # noqa: ANN001 - WorkflowExecution
        """Re-defer a reclaimed WAITING execution whose delay has not elapsed.

        Returns True when the execution was re-scheduled on the delayed queue
        (the caller must ack the job and not run the engine).
        """
        snapshot = execution.context_snapshot or {}
        delay_until_raw = snapshot.get("delay_until")
        if not delay_until_raw:
            return False
        try:
            delay_until = datetime.fromisoformat(str(delay_until_raw))
        except ValueError:
            return False
        remaining = (delay_until - datetime.now(UTC)).total_seconds()
        if remaining <= 0:
            return False
        if self.queue is None:
            return False
        await self.queue.enqueue_delayed(
            WorkflowJob(
                execution_id=job.execution_id,
                company_id=job.company_id,
                attempt=job.attempt,
            ),
            delay_seconds=remaining,
        )
        execution.heartbeat_at = datetime.now(UTC)
        logger.info(
            "execution %s deferred again for %.1fs (reclaimed before eta)",
            job.execution_id, remaining,
        )
        return True

    async def _schedule_resumption(self, execution, job: WorkflowJob) -> None:  # noqa: ANN001
        """Re-enqueue a WAITING execution on the delayed queue for its eta.

        Duplicate safety: the queued job carries the same deterministic
        payload; if it somehow lands twice, the second claim sees the
        execution running/terminal and is dropped without side effects.
        """
        if self.queue is None:
            logger.error("no queue available; execution %s stays waiting", job.execution_id)
            return
        delay_seconds = 0.0
        output = execution.output_metadata or {}
        raw = output.get("delay_seconds")
        if isinstance(raw, (int, float)) and raw > 0:
            delay_seconds = float(raw)
        await self.queue.enqueue_delayed(
            WorkflowJob(
                execution_id=job.execution_id,
                company_id=job.company_id,
                attempt=job.attempt,
            ),
            delay_seconds=delay_seconds,
        )
        logger.info(
            "execution %s scheduled for resumption in %.1fs",
            job.execution_id, delay_seconds,
        )

    async def _fail_job(self, job: WorkflowJob) -> None:
        try:
            async with self.session_factory() as session:
                from app.models import WorkflowExecution  # noqa: PLC0415

                execution = await session.get(WorkflowExecution, job.execution_id)
                if execution is not None and execution.status not in TERMINAL_EXECUTION_STATUSES:
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

    async def _monitoring_loop(self) -> None:
        """Scheduled platform monitoring: observe platforms, never generate.

        Sleeps first so a worker restart does not immediately crawl every
        platform; content generation is never triggered from here.
        """
        if not self.settings.platform_monitor_enabled:
            return
        interval = int(self.settings.platform_monitor_interval_seconds)
        logger.info("platform monitoring enabled (interval=%ds)", interval)
        while not self._stopping.is_set():
            await asyncio.sleep(interval)
            try:
                from app.services.platform_monitor_service import PlatformMonitorService  # noqa: PLC0415

                async with self.session_factory() as session:
                    results = await PlatformMonitorService(
                        session, self.registry, self.executor, self.master_key_box
                    ).run_cycle()
                    monitored = [item for item in results if item.get("status") == "ok"]
                    logger.info(
                        "platform monitoring cycle finished: %d platform(s), %d refreshed",
                        len(results), len(monitored),
                    )
            except Exception:  # noqa: BLE001 - the cycle must never kill the worker
                logger.exception("platform monitoring cycle failed")

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
