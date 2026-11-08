"""Workflow scheduling service: once / interval / cron triggers.

- Next-run calculation honors the schedule's IANA timezone (cron via croniter).
- Missed runs (after downtime) follow the schedule's policy: skip advances to
  the next future occurrence; run_once executes a single catch-up run.
- Duplicate prevention: every scheduled run carries a deterministic
  idempotency key (``sched:<id>:<period>``), so a scheduler that fires twice
  for the same period creates exactly one execution (unique index enforced).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from croniter import croniter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, NotFoundError
from app.core.queue import WorkflowQueue
from app.models import (
    Workflow,
    WorkflowDefinitionStatus,
    WorkflowSchedule,
    WorkflowScheduleMissedPolicy,
    WorkflowScheduleType,
    WorkflowTriggerType,
)
from app.services.audit_service import AuditService
from app.services.workflow_execution_service import WorkflowExecutionService

SCHEDULE_RESOURCE = "schedule"
MISSED_THRESHOLD = timedelta(seconds=60)


class WorkflowScheduleService:
    def __init__(self, session: AsyncSession, queue: WorkflowQueue | None) -> None:
        self.session = session
        self.queue = queue
        self.audit = AuditService(session)

    # --- CRUD -------------------------------------------------------------------
    async def create_schedule(
        self,
        workflow_id: uuid.UUID,
        *,
        company_id: uuid.UUID,
        name: str,
        schedule_type: WorkflowScheduleType,
        schedule_config: dict[str, Any],
        timezone_name: str = "UTC",
        missed_policy: WorkflowScheduleMissedPolicy = WorkflowScheduleMissedPolicy.SKIP,
        enabled: bool = True,
    ) -> WorkflowSchedule:
        workflow = await self.session.get(Workflow, workflow_id)
        if workflow is None or workflow.company_id != company_id:
            raise NotFoundError(
                "Workflow not found.", details=[{"field": "workflow_id", "value": str(workflow_id)}]
            )
        if workflow.status != WorkflowDefinitionStatus.PUBLISHED.value:
            raise ConflictError("Schedules can only be created for published workflows.")

        duplicate = await self.session.execute(
            select(WorkflowSchedule.id).where(
                WorkflowSchedule.workflow_id == workflow_id, WorkflowSchedule.name == name
            )
        )
        if duplicate.scalar_one_or_none() is not None:
            raise ConflictError(
                f"A schedule named '{name}' already exists for this workflow.",
                details=[{"field": "name", "value": name}],
            )

        self._validate_config(schedule_type, schedule_config)
        try:
            ZoneInfo(timezone_name)
        except Exception as exc:  # noqa: BLE001 - invalid tz names raise various errors
            raise ConflictError(
                f"Unknown timezone '{timezone_name}'.", details=[{"field": "timezone", "value": timezone_name}]
            ) from exc

        schedule = WorkflowSchedule(
            workflow_id=workflow_id,
            company_id=company_id,
            name=name,
            schedule_type=schedule_type.value,
            schedule_config=schedule_config,
            timezone=timezone_name,
            missed_policy=missed_policy.value,
            enabled=enabled,
        )
        if enabled:
            schedule.next_run_at = self._first_next_run(schedule)
        self.session.add(schedule)
        await self.session.flush()
        self.audit.record(
            event_type="workflow.schedule_created",
            action="create",
            company_id=company_id,
            resource_type=SCHEDULE_RESOURCE,
            resource_id=schedule.id,
            metadata={"workflow": workflow.name, "name": name, "type": schedule_type.value, "timezone": timezone_name},
        )
        await self.session.flush()
        return schedule

    async def get_schedule(self, schedule_id: uuid.UUID, *, company_id: uuid.UUID) -> WorkflowSchedule:
        schedule = await self.session.get(WorkflowSchedule, schedule_id)
        if schedule is None or schedule.company_id != company_id:
            raise NotFoundError(
                "Schedule not found.", details=[{"field": "schedule_id", "value": str(schedule_id)}]
            )
        return schedule

    async def list_schedules(self, workflow_id: uuid.UUID, *, company_id: uuid.UUID) -> list[WorkflowSchedule]:
        workflow = await self.session.get(Workflow, workflow_id)
        if workflow is None or workflow.company_id != company_id:
            raise NotFoundError(
                "Workflow not found.", details=[{"field": "workflow_id", "value": str(workflow_id)}]
            )
        return list(
            (
                await self.session.execute(
                    select(WorkflowSchedule)
                    .where(WorkflowSchedule.workflow_id == workflow_id)
                    .order_by(WorkflowSchedule.name)
                )
            ).scalars().all()
        )

    async def update_schedule(
        self,
        schedule_id: uuid.UUID,
        *,
        company_id: uuid.UUID,
        name: str | None = None,
        schedule_config: dict[str, Any] | None = None,
        timezone_name: str | None = None,
        enabled: bool | None = None,
    ) -> WorkflowSchedule:
        schedule = await self.get_schedule(schedule_id, company_id=company_id)
        if name is not None:
            schedule.name = name
        if schedule_config is not None:
            self._validate_config(WorkflowScheduleType(schedule.schedule_type), schedule_config)
            schedule.schedule_config = schedule_config
            schedule.next_run_at = self._first_next_run(schedule)
        if timezone_name is not None:
            try:
                ZoneInfo(timezone_name)
            except Exception as exc:  # noqa: BLE001
                raise ConflictError(
                    f"Unknown timezone '{timezone_name}'.",
                    details=[{"field": "timezone", "value": timezone_name}],
                ) from exc
            schedule.timezone = timezone_name
            schedule.next_run_at = self._first_next_run(schedule)
        if enabled is not None:
            schedule.enabled = enabled
            if enabled:
                schedule.next_run_at = self._first_next_run(schedule)
            else:
                # A disabled schedule has nothing pending.
                schedule.next_run_at = None
        self.audit.record(
            event_type="workflow.schedule_updated",
            action="update",
            company_id=company_id,
            resource_type=SCHEDULE_RESOURCE,
            resource_id=schedule.id,
            metadata={"enabled": schedule.enabled},
        )
        await self.session.flush()
        await self.session.refresh(schedule)
        return schedule

    async def delete_schedule(self, schedule_id: uuid.UUID, *, company_id: uuid.UUID) -> None:
        schedule = await self.get_schedule(schedule_id, company_id=company_id)
        snapshot = {"name": schedule.name, "workflow_id": str(schedule.workflow_id)}
        await self.session.delete(schedule)
        await self.session.flush()
        self.audit.record(
            event_type="workflow.schedule_deleted",
            action="delete",
            company_id=company_id,
            resource_type=SCHEDULE_RESOURCE,
            resource_id=schedule.id,
            metadata=snapshot,
        )

    # --- scheduling loop -----------------------------------------------------------
    async def collect_due_schedules(self, *, now: datetime | None = None) -> list[uuid.UUID]:
        """Fire all due schedules; returns created execution ids.

        Handles missed runs per policy and advances next_run_at. Duplicate
        prevention relies on the deterministic idempotency key + the unique
        index: firing the same period twice is a no-op.
        """
        now = now or datetime.now(UTC)
        created: list[uuid.UUID] = []
        rows = await self.session.execute(
            select(WorkflowSchedule).where(
                WorkflowSchedule.enabled.is_(True),
                WorkflowSchedule.next_run_at.isnot(None),
                WorkflowSchedule.next_run_at <= now,
            )
        )
        schedules = list(rows.scalars().all())
        for schedule in schedules:
            workflow = await self.session.get(Workflow, schedule.workflow_id)
            if workflow is None or workflow.status != WorkflowDefinitionStatus.PUBLISHED.value:
                # Paused/archived/deleted workflows do not fire; advance the clock.
                schedule.next_run_at = self._advance(schedule, schedule.next_run_at, now)
                await self.session.flush()
                continue

            scheduled_for = schedule.next_run_at
            missed = (now - scheduled_for) > MISSED_THRESHOLD
            should_execute = True
            if missed and schedule.missed_policy == WorkflowScheduleMissedPolicy.SKIP.value:
                should_execute = False
                self.audit.record(
                    event_type="workflow.schedule_missed",
                    action="skip",
                    company_id=schedule.company_id,
                    resource_type=SCHEDULE_RESOURCE,
                    resource_id=schedule.id,
                    metadata={"scheduled_for": scheduled_for.isoformat()},
                )

            if should_execute:
                execution, was_created = await self._execute_scheduled(schedule, workflow, scheduled_for)
                if was_created:
                    created.append(execution.id)
                schedule.last_run_at = now
                schedule.last_execution_id = execution.id
                schedule.last_status = execution.status

            schedule.next_run_at = self._advance(schedule, scheduled_for, now)
            await self.session.flush()
        return created

    async def _execute_scheduled(
        self, schedule: WorkflowSchedule, workflow: Workflow, scheduled_for: datetime
    ) -> tuple[Any, bool]:
        execution_service = WorkflowExecutionService(
            self.session, registry=None, executor=None, master_key_box=None, queue=self.queue  # type: ignore[arg-type]
        )
        idempotency_key = (
            f"sched:{schedule.id}"
            if schedule.schedule_type == WorkflowScheduleType.ONCE.value
            else f"sched:{schedule.id}:{int(scheduled_for.timestamp())}"
        )
        return await execution_service.execute_workflow(
            workflow.id,
            scope=_SystemScope(schedule.company_id),
            input_metadata={"scheduled_for": scheduled_for.isoformat(), "schedule": schedule.name},
            idempotency_key=idempotency_key,
            trigger_type=WorkflowTriggerType.SCHEDULE,
            trigger_reference=str(schedule.id),
        )

    # --- next-run math ------------------------------------------------------------
    def _first_next_run(self, schedule: WorkflowSchedule) -> datetime | None:
        """Initial next_run_at from creation time (used on create/enable)."""
        now = datetime.now(UTC)
        return self._advance(schedule, None, now)

    def _advance(
        self, schedule: WorkflowSchedule, current: datetime | None, now: datetime
    ) -> datetime | None:
        schedule_type = WorkflowScheduleType(schedule.schedule_type)
        tz = ZoneInfo(schedule.timezone)
        now_local = now.astimezone(tz)

        if schedule_type is WorkflowScheduleType.ONCE:
            # Already fired? A 'once' schedule never fires twice.
            if schedule.last_run_at is not None:
                return None
            at = schedule.schedule_config.get("at")
            if not at:
                return None
            try:
                target = datetime.fromisoformat(str(at))
            except ValueError:
                return None
            if target.tzinfo is None:
                target = target.replace(tzinfo=tz)
            # A past-but-unfired 'once' target is due immediately (the poller
            # fires it once; the idempotency key guards duplicates).
            return target

        if schedule_type is WorkflowScheduleType.INTERVAL:
            interval = int(schedule.schedule_config.get("interval_seconds", 0))
            if interval <= 0:
                return None
            base = current or now
            candidate = base + timedelta(seconds=interval)
            # Jump past missed intervals so the next run is in the future.
            while candidate <= now:
                candidate += timedelta(seconds=interval)
            return candidate

        # CRON
        expression = schedule.schedule_config.get("cron")
        if not expression:
            return None
        try:
            iterator = croniter(str(expression), now_local)
            next_local = iterator.get_next(datetime)
        except (ValueError, KeyError) as exc:
            raise ConflictError(
                f"Invalid cron expression '{expression}'.",
                details=[{"field": "schedule_config.cron"}],
            ) from exc
        return next_local.astimezone(UTC)

    @staticmethod
    def _validate_config(schedule_type: WorkflowScheduleType, config: dict[str, Any]) -> None:
        if not isinstance(config, dict):
            raise ConflictError("schedule_config must be an object.")
        if schedule_type is WorkflowScheduleType.ONCE:
            if not isinstance(config.get("at"), str):
                raise ConflictError("once schedules require an 'at' ISO-8601 timestamp.")
            try:
                datetime.fromisoformat(str(config["at"]))
            except ValueError as exc:
                raise ConflictError("'at' must be a valid ISO-8601 timestamp.") from exc
        elif schedule_type is WorkflowScheduleType.INTERVAL:
            interval = config.get("interval_seconds")
            if not isinstance(interval, int) or isinstance(interval, bool) or interval <= 0:
                raise ConflictError("interval schedules require a positive 'interval_seconds'.")
            if interval < 10:
                raise ConflictError("'interval_seconds' must be at least 10.")
        else:
            expression = config.get("cron")
            if not isinstance(expression, str) or not expression.strip():
                raise ConflictError("cron schedules require a 'cron' expression.")
            try:
                croniter(expression)  # validation only
            except (ValueError, KeyError) as exc:
                raise ConflictError(f"Invalid cron expression '{expression}'.") from exc


class _SystemScope:
    """Scheduler-internal scope: the schedule's own company, system actor."""

    def __init__(self, company_id: uuid.UUID) -> None:
        self.company_id = company_id
