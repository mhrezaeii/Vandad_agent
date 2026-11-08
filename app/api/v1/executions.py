"""Execution monitoring endpoints: list, detail, cancel, steps, logs.

Plus schedule management (PATCH/DELETE /api/v1/schedules/{id}).
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status

from app.api.deps import (
    company_scope,
    get_workflow_execution_service,
    get_workflow_service,
    pagination_params,
)
from app.core.exceptions import UnprocessableEntityError
from app.schemas.workflow import (
    ScheduleListResponse,
    ScheduleRead,
    ScheduleUpdate,
    WorkflowExecutionListResponse,
    WorkflowExecutionRead,
    WorkflowLogListResponse,
    WorkflowStepListResponse,
)
from app.security.authorization import CompanyScope
from app.services.workflow_execution_service import WorkflowExecutionService
from app.services.workflow_service import WorkflowService

router = APIRouter(prefix="/executions", tags=["executions"])
schedule_router = APIRouter(prefix="/schedules", tags=["schedules"])

EXECUTION_STATUSES = {
    "pending", "queued", "running", "waiting", "completed", "failed", "cancelled", "timed_out", "succeeded",
}


@router.get("", response_model=WorkflowExecutionListResponse)
async def list_executions(
    scope: Annotated[CompanyScope, Depends(company_scope)],
    service: Annotated[WorkflowExecutionService, Depends(get_workflow_execution_service)],
    pagination: Annotated[tuple[int, int], Depends(pagination_params)],
    workflow_id: uuid.UUID | None = None,
    execution_status: str | None = Query(default=None, alias="status"),
) -> WorkflowExecutionListResponse:
    if execution_status is not None and execution_status not in EXECUTION_STATUSES:
        raise UnprocessableEntityError(
            "Unknown execution status filter.",
            details=[{"field": "status", "value": execution_status}],
        )
    limit, offset = pagination
    items, total = await service.list_executions(
        company_id=scope.company_id,
        workflow_id=workflow_id,
        status=execution_status,
        limit=limit,
        offset=offset,
    )
    return WorkflowExecutionListResponse(
        items=[WorkflowExecutionRead.model_validate(item) for item in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{execution_id}", response_model=WorkflowExecutionRead)
async def get_execution(
    execution_id: uuid.UUID,
    scope: Annotated[CompanyScope, Depends(company_scope)],
    service: Annotated[WorkflowExecutionService, Depends(get_workflow_execution_service)],
) -> WorkflowExecutionRead:
    execution = await service.get_execution(execution_id, company_id=scope.company_id)
    return WorkflowExecutionRead.model_validate(execution)


@router.post("/{execution_id}/cancel", response_model=WorkflowExecutionRead)
async def cancel_execution(
    execution_id: uuid.UUID,
    scope: Annotated[CompanyScope, Depends(company_scope)],
    service: Annotated[WorkflowExecutionService, Depends(get_workflow_execution_service)],
) -> WorkflowExecutionRead:
    execution = await service.cancel_execution(execution_id, scope=scope)
    return WorkflowExecutionRead.model_validate(execution)


@router.get("/{execution_id}/steps", response_model=WorkflowStepListResponse)
async def list_steps(
    execution_id: uuid.UUID,
    scope: Annotated[CompanyScope, Depends(company_scope)],
    service: Annotated[WorkflowExecutionService, Depends(get_workflow_execution_service)],
) -> WorkflowStepListResponse:
    await service.get_execution(execution_id, company_id=scope.company_id)
    steps = await service.list_steps(execution_id)
    return WorkflowStepListResponse(
        items=[WorkflowStepRead.model_validate(step) for step in steps],
        total=len(steps),
    )


@router.get("/{execution_id}/logs", response_model=WorkflowLogListResponse)
async def list_logs(
    execution_id: uuid.UUID,
    scope: Annotated[CompanyScope, Depends(company_scope)],
    service: Annotated[WorkflowExecutionService, Depends(get_workflow_execution_service)],
    pagination: Annotated[tuple[int, int], Depends(pagination_params)],
) -> WorkflowLogListResponse:
    await service.get_execution(execution_id, company_id=scope.company_id)
    limit, offset = pagination
    from sqlalchemy import func, select  # noqa: PLC0415

    from app.models import WorkflowExecutionLog  # noqa: PLC0415

    total = int(
        (
            await service.session.execute(
                select(func.count())
                .select_from(WorkflowExecutionLog)
                .where(WorkflowExecutionLog.execution_id == execution_id)
            )
        ).scalar_one()
    )
    logs = list(
        (
            await service.session.execute(
                select(WorkflowExecutionLog)
                .where(WorkflowExecutionLog.execution_id == execution_id)
                .order_by(WorkflowExecutionLog.created_at, WorkflowExecutionLog.id)
                .limit(limit)
                .offset(offset)
            )
        ).scalars().all()
    )
    return WorkflowLogListResponse(
        items=[WorkflowLogRead.model_validate(log) for log in logs],
        total=total,
    )


# --- schedules -----------------------------------------------------------------------


@schedule_router.patch("/{schedule_id}", response_model=ScheduleRead)
async def update_schedule(
    schedule_id: uuid.UUID,
    payload: ScheduleUpdate,
    scope: Annotated[CompanyScope, Depends(company_scope)],
    service: Annotated[WorkflowService, Depends(get_workflow_service)],
) -> ScheduleRead:
    from app.services.workflow_schedule_service import WorkflowScheduleService  # noqa: PLC0415

    scheduler = WorkflowScheduleService(service.session, queue=None)
    schedule = await scheduler.update_schedule(
        schedule_id,
        company_id=scope.company_id,
        name=payload.name,
        schedule_config=payload.schedule_config,
        timezone_name=payload.timezone,
        enabled=payload.enabled,
    )
    return ScheduleRead.model_validate(schedule)


@schedule_router.delete("/{schedule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_schedule(
    schedule_id: uuid.UUID,
    scope: Annotated[CompanyScope, Depends(company_scope)],
    service: Annotated[WorkflowService, Depends(get_workflow_service)],
) -> Response:
    from app.services.workflow_schedule_service import WorkflowScheduleService  # noqa: PLC0415

    scheduler = WorkflowScheduleService(service.session, queue=None)
    await scheduler.delete_schedule(schedule_id, company_id=scope.company_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@schedule_router.get("", response_model=ScheduleListResponse)
async def list_all_schedules(
    scope: Annotated[CompanyScope, Depends(company_scope)],
    service: Annotated[WorkflowService, Depends(get_workflow_service)],
    pagination: Annotated[tuple[int, int], Depends(pagination_params)],
) -> ScheduleListResponse:
    """All schedules of the acting company (cross-workflow view)."""
    from sqlalchemy import func, select  # noqa: PLC0415

    from app.models import WorkflowSchedule  # noqa: PLC0415
    from app.services.workflow_schedule_service import WorkflowScheduleService  # noqa: PLC0415

    scheduler = WorkflowScheduleService(service.session, queue=None)
    limit, offset = pagination
    total = int(
        (
            await service.session.execute(
                select(func.count())
                .select_from(WorkflowSchedule)
                .where(WorkflowSchedule.company_id == scope.company_id)
            )
        ).scalar_one()
    )
    rows = list(
        (
            await service.session.execute(
                select(WorkflowSchedule)
                .where(WorkflowSchedule.company_id == scope.company_id)
                .order_by(WorkflowSchedule.next_run_at)
                .limit(limit)
                .offset(offset)
            )
        ).scalars().all()
    )
    _ = scheduler
    return ScheduleListResponse(items=[ScheduleRead.model_validate(row) for row in rows], total=total)
