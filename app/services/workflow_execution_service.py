"""Workflow execution service: trigger, idempotency, cancel, recovery, queries.

Executions are created against a *published* version only, enqueued onto the
Redis queue, and processed by workers. The PostgreSQL row is the source of
truth; the queue carries only (execution_id, company_id, attempt) pointers.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, NotFoundError
from app.core.queue import WorkflowJob, WorkflowQueue
from app.integrations.providers import ProviderExecutor, ProviderRegistry
from app.models import (
    Workflow,
    WorkflowDefinitionStatus,
    WorkflowExecution,
    WorkflowExecutionStep,
    WorkflowStatus,
    WorkflowTriggerType,
    WorkflowVersion,
    WorkflowVersionStatus,
)
from app.security.authorization import CompanyScope
from app.services.audit_service import AuditService
from app.workflows.engine import WorkflowEngine

logger = logging.getLogger("app.workflow_execution")

EXECUTION_RESOURCE = "execution"
TERMINAL_STATUSES = frozenset(
    {WorkflowStatus.COMPLETED.value, WorkflowStatus.FAILED.value, WorkflowStatus.CANCELLED.value,
     WorkflowStatus.TIMED_OUT.value, WorkflowStatus.SUCCEEDED.value}
)
DEFAULT_MAX_QUEUE_ATTEMPTS = 5


class WorkflowExecutionService:
    def __init__(
        self,
        session: AsyncSession,
        registry: ProviderRegistry,
        executor: ProviderExecutor,
        master_key_box,
        queue: WorkflowQueue | None = None,  # noqa: ANN001 - MasterKeyBox
    ) -> None:
        self.session = session
        self.registry = registry
        self.executor = executor
        self._keys = master_key_box
        self.queue = queue
        self.audit = AuditService(session)

    # --- trigger -----------------------------------------------------------------
    async def execute_workflow(
        self,
        workflow_id: uuid.UUID,
        *,
        scope: CompanyScope,
        input_metadata: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        trigger_type: WorkflowTriggerType = WorkflowTriggerType.MANUAL,
        trigger_reference: str | None = None,
        enqueue: bool = True,
    ) -> tuple[WorkflowExecution, bool]:
        """Create and enqueue an execution; returns (execution, created).

        With an ``idempotency_key`` that already exists for the company, the
        existing execution is returned (created=False) and nothing is enqueued.
        """
        workflow = await self.session.get(Workflow, workflow_id)
        if workflow is None or workflow.company_id != scope.company_id:
            raise NotFoundError(
                "Workflow not found.", details=[{"field": "workflow_id", "value": str(workflow_id)}]
            )
        if workflow.status != WorkflowDefinitionStatus.PUBLISHED.value:
            raise ConflictError(
                "Only published workflows can be executed.",
                details=[{"field": "status", "value": workflow.status}],
            )
        version = await self._published_version(workflow)

        if idempotency_key:
            existing = await self._find_by_idempotency_key(scope.company_id, idempotency_key)
            if existing is not None:
                return existing, False

        execution = WorkflowExecution(
            workflow_name=workflow.name,
            status=WorkflowStatus.PENDING.value,
            input_metadata=input_metadata or {},
            workflow_id=workflow.id,
            workflow_version_id=version.id,
            company_id=scope.company_id,
            trigger_type=trigger_type.value,
            trigger_reference=trigger_reference,
            idempotency_key=idempotency_key,
            timeout_seconds=int((workflow.default_execution_settings or {}).get("timeout_seconds") or 0) or None,
        )
        self.session.add(execution)
        await self.session.flush()

        self.audit.record(
            event_type="workflow.executed",
            action="execute",
            company_id=scope.company_id,
            resource_type=EXECUTION_RESOURCE,
            resource_id=execution.id,
            metadata={
                "workflow": workflow.name,
                "version": version.version,
                "trigger": trigger_type.value,
                "idempotency_key": idempotency_key,
            },
        )
        if enqueue and self.queue is not None:
            await self._enqueue(execution)
        await self.session.flush()
        return execution, True

    async def _enqueue(self, execution: WorkflowExecution) -> None:
        if self.queue is None:
            raise ConflictError("The workflow queue is not available.")
        execution.queue_attempts = (execution.queue_attempts or 0) + 1
        execution.status = WorkflowStatus.QUEUED.value
        await self.queue.enqueue(
            WorkflowJob(execution_id=execution.id, company_id=execution.company_id)
        )

    # --- cancel ---------------------------------------------------------------------
    async def cancel_execution(self, execution_id: uuid.UUID, *, scope: CompanyScope) -> WorkflowExecution:
        execution = await self.get_execution(execution_id, company_id=scope.company_id)
        if execution.status in TERMINAL_STATUSES:
            raise ConflictError(
                f"Execution is already terminal ('{execution.status}').",
                details=[{"field": "status", "value": execution.status}],
            )
        if execution.status in (WorkflowStatus.PENDING.value, WorkflowStatus.QUEUED.value, WorkflowStatus.WAITING.value):
            execution.status = WorkflowStatus.CANCELLED.value
            execution.completed_at = datetime.now(UTC)
            execution.context_snapshot = None
        else:  # running: request cooperative cancellation between nodes
            execution.cancel_requested = True
        self.audit.record(
            event_type="workflow.execution_cancelled",
            action="cancel",
            company_id=scope.company_id,
            resource_type=EXECUTION_RESOURCE,
            resource_id=execution.id,
            metadata={"status": execution.status},
        )
        await self.session.flush()
        await self.session.refresh(execution)
        return execution

    # --- queries ---------------------------------------------------------------------
    async def get_execution(self, execution_id: uuid.UUID, *, company_id: uuid.UUID) -> WorkflowExecution:
        execution = await self.session.get(WorkflowExecution, execution_id)
        if execution is None or execution.company_id != company_id:
            raise NotFoundError(
                "Execution not found.", details=[{"field": "execution_id", "value": str(execution_id)}]
            )
        return execution

    async def list_executions(
        self,
        *,
        company_id: uuid.UUID,
        workflow_id: uuid.UUID | None = None,
        status: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[WorkflowExecution], int]:
        conditions = [WorkflowExecution.company_id == company_id]
        if workflow_id is not None:
            conditions.append(WorkflowExecution.workflow_id == workflow_id)
        if status is not None:
            conditions.append(WorkflowExecution.status == status)
        total = int(
            (
                await self.session.execute(
                    select(func.count()).select_from(WorkflowExecution).where(*conditions)
                )
            ).scalar_one()
        )
        items = list(
            (
                await self.session.execute(
                    select(WorkflowExecution)
                    .where(*conditions)
                    .order_by(WorkflowExecution.created_at.desc(), WorkflowExecution.id)
                    .limit(limit)
                    .offset(offset)
                )
            ).scalars().all()
        )
        return items, total

    async def list_steps(self, execution_id: uuid.UUID) -> list[WorkflowExecutionStep]:
        return list(
            (
                await self.session.execute(
                    select(WorkflowExecutionStep)
                    .where(WorkflowExecutionStep.execution_id == execution_id)
                    .order_by(WorkflowExecutionStep.started_at, WorkflowExecutionStep.attempt)
                )
            ).scalars().all()
        )

    # --- recovery ---------------------------------------------------------------------
    async def recover_abandoned_executions(
        self, *, stale_after_seconds: float = 120, requeue_limit: int = 50
    ) -> dict[str, int]:
        """Find executions whose worker died and recover them safely.

        An execution is abandoned when it is running/waiting with a stale
        heartbeat. It is re-queued only when no completed non-idempotent
        action step exists (those may already have side effects); otherwise it
        is marked failed. Returns counters for observability.
        """
        threshold = datetime.now(UTC) - timedelta(seconds=stale_after_seconds)
        rows = await self.session.execute(
            select(WorkflowExecution).where(
                WorkflowExecution.status.in_(
                    [WorkflowStatus.RUNNING.value, WorkflowStatus.WAITING.value]
                ),
                WorkflowExecution.heartbeat_at.isnot(None),
                WorkflowExecution.heartbeat_at < threshold,
            ).limit(requeue_limit)
        )
        abandoned = list(rows.scalars().all())
        requeued = failed = 0
        for execution in abandoned:
            has_side_effects = await self._has_completed_non_idempotent_steps(execution.id)
            if has_side_effects:
                execution.status = WorkflowStatus.FAILED.value
                execution.error_message = "Interrupted by a worker crash after non-idempotent steps ran."
                failed += 1
            else:
                execution.status = WorkflowStatus.QUEUED.value
                execution.queue_attempts = (execution.queue_attempts or 0) + 1
                execution.heartbeat_at = None
                if self.queue is not None and execution.queue_attempts <= DEFAULT_MAX_QUEUE_ATTEMPTS:
                    await self.queue.enqueue(
                        WorkflowJob(
                            execution_id=execution.id,
                            company_id=execution.company_id,
                            attempt=execution.queue_attempts,
                        )
                    )
                requeued += 1
        if abandoned:
            logger.warning(
                "recovered %d abandoned execution(s): requeued=%d failed=%d",
                len(abandoned), requeued, failed,
            )
        await self.session.flush()
        return {"abandoned": len(abandoned), "requeued": requeued, "failed": failed}

    async def _has_completed_non_idempotent_steps(self, execution_id: uuid.UUID) -> bool:
        execution = await self.session.get(WorkflowExecution, execution_id)
        if execution is None or execution.workflow_version_id is None:
            return False
        rows = await self.session.execute(
            select(WorkflowNode.execution_settings)
            .select_from(WorkflowExecutionStep)
            .join(
                WorkflowNode,
                (WorkflowNode.node_key == WorkflowExecutionStep.node_key)
                & (WorkflowNode.version_id == execution.workflow_version_id),
            )
            .where(
                WorkflowExecutionStep.execution_id == execution_id,
                WorkflowExecutionStep.status == WorkflowStepStatus.COMPLETED.value,
            )
        )
        for (settings,) in rows.all():
            if not (settings or {}).get("idempotent", True):
                return True
        return False

    # --- internal -----------------------------------------------------------------------
    async def _published_version(self, workflow: Workflow) -> WorkflowVersion:
        if workflow.current_version_id is None:
            raise ConflictError("Workflow has no published version.")
        version = await self.session.get(WorkflowVersion, workflow.current_version_id)
        if version is None or version.status != WorkflowVersionStatus.PUBLISHED.value:
            raise ConflictError("Workflow has no published version.")
        return version

    async def _find_by_idempotency_key(
        self, company_id: uuid.UUID, idempotency_key: str
    ) -> WorkflowExecution | None:
        result = await self.session.execute(
            select(WorkflowExecution).where(
                WorkflowExecution.company_id == company_id,
                WorkflowExecution.idempotency_key == idempotency_key,
            )
        )
        return result.scalar_one_or_none()
