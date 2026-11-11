"""Tenant-scoped task lifecycle and execution-attempt management.

Task creation records intent only. Dispatch remains the Orchestrator's job;
start_run refuses to execute until the selected agent definition is dispatch-ready.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents import AgentRegistry, UnknownAgentError
from app.core.exceptions import ConflictError, NotFoundError, UnprocessableEntityError
from app.models import ActorType, AgentTask, AgentTaskRun, CompanyAgent
from app.models.agent_task import AgentTaskStatus
from app.services.audit_service import AuditService


class AgentTaskService:
    def __init__(self, session: AsyncSession, registry: AgentRegistry) -> None:
        self.session = session
        self.registry = registry
        self.audit = AuditService(session)

    @staticmethod
    def _same_request(task: AgentTask, *, agent_key: str, capability: str, title: str,
                      input_payload: dict[str, Any], priority: int, max_attempts: int) -> bool:
        return (
            task.agent_key == agent_key
            and task.capability == capability
            and task.title == title
            and task.input_payload == input_payload
            and task.priority == priority
            and task.max_attempts == max_attempts
        )

    async def create_task(
        self, company_id: uuid.UUID, *, requested_by: str, agent_key: str,
        capability: str, title: str, input_payload: dict[str, Any],
        idempotency_key: str | None, priority: int, max_attempts: int,
    ) -> AgentTask:
        try:
            definition = self.registry.get(agent_key)
        except UnknownAgentError as exc:
            raise NotFoundError("Agent definition not found.") from exc
        if capability not in definition.capabilities:
            raise UnprocessableEntityError(
                "The requested capability is not registered for this agent.",
                details=[{"field": "capability", "value": capability}],
            )

        agent = await self.session.scalar(
            select(CompanyAgent).where(
                CompanyAgent.company_id == company_id,
                CompanyAgent.agent_key == agent_key,
            )
        )
        if agent is None or agent.status != "active":
            raise ConflictError("The selected agent is not active for this company.")

        if idempotency_key:
            existing = await self.session.scalar(
                select(AgentTask).where(
                    AgentTask.company_id == company_id,
                    AgentTask.idempotency_key == idempotency_key,
                )
            )
            if existing is not None:
                if not self._same_request(
                    existing, agent_key=agent_key, capability=capability, title=title,
                    input_payload=input_payload, priority=priority, max_attempts=max_attempts,
                ):
                    raise ConflictError("Idempotency key was already used for a different task.")
                return existing

        task = AgentTask(
            company_id=company_id,
            agent_key=agent_key,
            capability=capability,
            title=title,
            input_payload=input_payload,
            idempotency_key=idempotency_key,
            requested_by=requested_by,
            priority=priority,
            max_attempts=max_attempts,
            status=AgentTaskStatus.PENDING.value,
        )
        self.session.add(task)
        try:
            await self.session.flush()
        except IntegrityError as exc:
            await self.session.rollback()
            if idempotency_key:
                existing = await self.session.scalar(
                    select(AgentTask).where(
                        AgentTask.company_id == company_id,
                        AgentTask.idempotency_key == idempotency_key,
                    )
                )
                if existing is not None and self._same_request(
                    existing, agent_key=agent_key, capability=capability, title=title,
                    input_payload=input_payload, priority=priority, max_attempts=max_attempts,
                ):
                    return existing
            raise ConflictError("Task creation conflicted with another request.") from exc

        self.audit.record(
            event_type="agent.task.created",
            action="create_task",
            actor_type=ActorType.USER,
            actor_id=requested_by,
            company_id=company_id,
            resource_type="agent_task",
            resource_id=task.id,
            metadata={"agent_key": agent_key, "capability": capability, "priority": priority},
        )
        return task

    async def list_tasks(
        self, company_id: uuid.UUID, *, limit: int, offset: int,
        status: str | None = None, agent_key: str | None = None,
    ) -> tuple[list[AgentTask], int]:
        filters = [AgentTask.company_id == company_id]
        if status is not None:
            filters.append(AgentTask.status == status)
        if agent_key is not None:
            filters.append(AgentTask.agent_key == agent_key)
        rows = await self.session.scalars(
            select(AgentTask).where(*filters)
            .order_by(AgentTask.created_at.desc(), AgentTask.id.desc())
            .limit(limit).offset(offset)
        )
        total = await self.session.scalar(select(func.count()).select_from(AgentTask).where(*filters))
        return list(rows.all()), int(total or 0)

    async def get_task(self, company_id: uuid.UUID, task_id: uuid.UUID) -> AgentTask:
        task = await self.session.scalar(
            select(AgentTask).where(AgentTask.id == task_id, AgentTask.company_id == company_id)
        )
        if task is None:
            raise NotFoundError("Agent task not found.")
        return task

    async def list_runs(self, company_id: uuid.UUID, task_id: uuid.UUID) -> list[AgentTaskRun]:
        await self.get_task(company_id, task_id)
        rows = await self.session.scalars(
            select(AgentTaskRun).where(
                AgentTaskRun.task_id == task_id, AgentTaskRun.company_id == company_id
            ).order_by(AgentTaskRun.attempt_number.asc())
        )
        return list(rows.all())

    async def cancel_task(self, company_id: uuid.UUID, task_id: uuid.UUID, *, requested_by: str) -> AgentTask:
        task = await self.session.scalar(
            select(AgentTask).where(
                AgentTask.id == task_id, AgentTask.company_id == company_id
            ).with_for_update()
        )
        if task is None:
            raise NotFoundError("Agent task not found.")
        if task.status == AgentTaskStatus.CANCELLED.value:
            return task
        if task.status in {AgentTaskStatus.COMPLETED.value, AgentTaskStatus.FAILED.value}:
            raise ConflictError("A terminal task cannot be cancelled.")
        if task.status == AgentTaskStatus.RUNNING.value:
            task.cancel_requested = True
        else:
            task.status = AgentTaskStatus.CANCELLED.value
            task.completed_at = datetime.now(UTC)
        await self.session.flush()
        self.audit.record(
            event_type="agent.task.cancel_requested",
            action="cancel_task",
            actor_type=ActorType.USER,
            actor_id=requested_by,
            company_id=company_id,
            resource_type="agent_task",
            resource_id=task.id,
            metadata={"status": task.status, "cancel_requested": task.cancel_requested},
        )
        return task

    async def retry_task(self, company_id: uuid.UUID, task_id: uuid.UUID, *, requested_by: str) -> AgentTask:
        task = await self.session.scalar(
            select(AgentTask).where(
                AgentTask.id == task_id, AgentTask.company_id == company_id
            ).with_for_update()
        )
        if task is None:
            raise NotFoundError("Agent task not found.")
        if task.status != AgentTaskStatus.FAILED.value:
            raise ConflictError("Only failed tasks can be retried.")
        if task.attempt_count >= task.max_attempts:
            raise ConflictError("The task has reached its maximum number of attempts.")
        task.status = AgentTaskStatus.QUEUED.value
        task.cancel_requested = False
        task.completed_at = None
        task.error_code = None
        task.error_message = None
        await self.session.flush()
        self.audit.record(
            event_type="agent.task.requeued",
            action="retry_task",
            actor_type=ActorType.USER,
            actor_id=requested_by,
            company_id=company_id,
            resource_type="agent_task",
            resource_id=task.id,
            metadata={"attempt_count": task.attempt_count, "max_attempts": task.max_attempts},
        )
        return task

    async def start_run(self, company_id: uuid.UUID, task_id: uuid.UUID, *, worker_id: str) -> AgentTaskRun:
        task = await self.session.scalar(
            select(AgentTask).where(
                AgentTask.id == task_id, AgentTask.company_id == company_id
            ).with_for_update()
        )
        if task is None:
            raise NotFoundError("Agent task not found.")
        if task.status not in {AgentTaskStatus.PENDING.value, AgentTaskStatus.QUEUED.value}:
            raise ConflictError("Task is not in a dispatchable state.")
        if task.cancel_requested:
            raise ConflictError("Task cancellation has been requested.")
        if task.attempt_count >= task.max_attempts:
            raise ConflictError("The task has reached its maximum number of attempts.")

        definition = self.registry.get(task.agent_key)
        if not definition.dispatch_ready:
            raise ConflictError("The selected agent is not dispatch-ready yet.")
        agent = await self.session.scalar(
            select(CompanyAgent).where(
                CompanyAgent.company_id == company_id,
                CompanyAgent.agent_key == task.agent_key,
            )
        )
        if agent is None or agent.status != "active":
            raise ConflictError("The selected company agent is not active.")

        now = datetime.now(UTC)
        attempt = task.attempt_count + 1
        run = AgentTaskRun(
            task_id=task.id,
            company_id=company_id,
            attempt_number=attempt,
            status="running",
            input_snapshot=dict(task.input_payload),
            worker_id=worker_id,
            started_at=now,
            heartbeat_at=now,
        )
        task.status = AgentTaskStatus.RUNNING.value
        task.attempt_count = attempt
        task.started_at = now
        task.completed_at = None
        self.session.add(run)
        await self.session.flush()
        self.audit.record(
            event_type="agent.task.run_started",
            action="start_run",
            actor_type=ActorType.AGENT,
            actor_id=task.agent_key,
            company_id=company_id,
            resource_type="agent_task_run",
            resource_id=run.id,
            metadata={"task_id": str(task.id), "attempt_number": attempt},
        )
        return run

    async def complete_run(
        self, company_id: uuid.UUID, task_id: uuid.UUID, run_id: uuid.UUID,
        *, result_payload: dict[str, Any],
    ) -> AgentTask:
        task, run = await self._lock_task_and_run(company_id, task_id, run_id)
        now = datetime.now(UTC)
        if task.cancel_requested:
            run.status = "cancelled"
            task.status = AgentTaskStatus.CANCELLED.value
        else:
            run.status = "completed"
            run.result_payload = result_payload
            task.status = AgentTaskStatus.COMPLETED.value
            task.result_payload = result_payload
        run.completed_at = now
        task.completed_at = now
        await self.session.flush()
        self.audit.record(
            event_type="agent.task.run_completed" if run.status == "completed" else "agent.task.cancelled",
            action="complete_run",
            actor_type=ActorType.AGENT,
            actor_id=task.agent_key,
            company_id=company_id,
            resource_type="agent_task",
            resource_id=task.id,
            metadata={"run_id": str(run.id), "status": task.status},
        )
        return task

    async def fail_run(
        self, company_id: uuid.UUID, task_id: uuid.UUID, run_id: uuid.UUID,
        *, error_code: str, error_message: str,
    ) -> AgentTask:
        task, run = await self._lock_task_and_run(company_id, task_id, run_id)
        now = datetime.now(UTC)
        run.status = "cancelled" if task.cancel_requested else "failed"
        task.status = AgentTaskStatus.CANCELLED.value if task.cancel_requested else AgentTaskStatus.FAILED.value
        run.error_code = error_code
        run.error_message = error_message
        task.error_code = error_code
        task.error_message = error_message
        run.completed_at = now
        task.completed_at = now
        await self.session.flush()
        self.audit.record(
            event_type="agent.task.run_failed" if run.status == "failed" else "agent.task.cancelled",
            action="fail_run",
            actor_type=ActorType.AGENT,
            actor_id=task.agent_key,
            company_id=company_id,
            resource_type="agent_task",
            resource_id=task.id,
            metadata={"run_id": str(run.id), "error_code": error_code, "status": task.status},
        )
        return task

    async def _lock_task_and_run(
        self, company_id: uuid.UUID, task_id: uuid.UUID, run_id: uuid.UUID
    ) -> tuple[AgentTask, AgentTaskRun]:
        task = await self.session.scalar(
            select(AgentTask).where(
                AgentTask.id == task_id, AgentTask.company_id == company_id
            ).with_for_update()
        )
        if task is None:
            raise NotFoundError("Agent task not found.")
        run = await self.session.scalar(
            select(AgentTaskRun).where(
                AgentTaskRun.id == run_id,
                AgentTaskRun.task_id == task_id,
                AgentTaskRun.company_id == company_id,
            ).with_for_update()
        )
        if run is None:
            raise NotFoundError("Agent task run not found.")
        if task.status != AgentTaskStatus.RUNNING.value or run.status != "running":
            raise ConflictError("Only the active run can change task execution state.")
        return task, run
