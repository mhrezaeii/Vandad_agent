"""Workflow management endpoints: CRUD, lifecycle, validation, executions, schedules."""

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
from app.core.exceptions import NotFoundError, UnprocessableEntityError
from app.security.authorization import CompanyScope
from app.schemas.workflow import (
    ScheduleCreate,
    ScheduleListResponse,
    ScheduleRead,
    ScheduleUpdate,
    WorkflowCreate,
    WorkflowDetailRead,
    WorkflowExecutionListResponse,
    WorkflowExecutionRead,
    WorkflowListResponse,
    WorkflowLogListResponse,
    WorkflowStepListResponse,
    WorkflowUpdate,
    WorkflowValidationResponse,
)
from app.services.workflow_execution_service import WorkflowExecutionService
from app.services.workflow_service import WorkflowService
from app.workflows.validation import WorkflowValidationError

router = APIRouter(prefix="/workflows", tags=["workflows"])


def _to_node_dicts(payload: WorkflowCreate | WorkflowUpdate):
    nodes = None
    edges = None
    if getattr(payload, "nodes", None) is not None:
        nodes = [node.model_dump() for node in payload.nodes]
    if getattr(payload, "edges", None) is not None:
        edges = [edge.model_dump() for edge in payload.edges]
    return nodes, edges


@router.post("", response_model=WorkflowDetailRead, status_code=status.HTTP_201_CREATED)
async def create_workflow(
    payload: WorkflowCreate,
    scope: Annotated[CompanyScope, Depends(company_scope)],
    service: Annotated[WorkflowService, Depends(get_workflow_service)],
) -> WorkflowDetailRead:
    nodes, edges = _to_node_dicts(payload)
    workflow = await service.create_workflow(
        scope=scope,
        name=payload.name,
        description=payload.description,
        nodes=nodes or [],
        edges=edges or [],
        default_execution_settings=payload.default_execution_settings,
    )
    return await _detail(service, workflow.id, scope)


@router.get("", response_model=WorkflowListResponse)
async def list_workflows(
    scope: Annotated[CompanyScope, Depends(company_scope)],
    service: Annotated[WorkflowService, Depends(get_workflow_service)],
    pagination: Annotated[tuple[int, int], Depends(pagination_params)],
    workflow_status: str | None = Query(default=None, alias="status"),
) -> WorkflowListResponse:
    if workflow_status is not None and workflow_status not in {"draft", "published", "paused", "archived"}:
        raise UnprocessableEntityError(
            "Unknown workflow status filter.", details=[{"field": "status", "value": workflow_status}]
        )
    limit, offset = pagination
    items, total = await service.list_workflows(
        company_id=scope.company_id, status=workflow_status, limit=limit, offset=offset
    )
    return WorkflowListResponse(
        items=[WorkflowRead.model_validate(item) for item in items],
        total=total,
        limit=limit,
        offset=offset,
    )


async def _detail(service: WorkflowService, workflow_id: uuid.UUID, scope: CompanyScope) -> WorkflowDetailRead:
    workflow = await service.get_workflow(workflow_id, company_id=scope.company_id)
    detail = WorkflowDetailRead.model_validate(workflow)
    version_id = workflow.current_version_id
    if version_id is not None:
        from app.workflows.validation import load_version_graph

        nodes, edges = await load_version_graph(service.session, version_id)
        detail.nodes = [WorkflowNodeRead.model_validate(node) for node in nodes]
        detail.edges = [
            WorkflowEdgeRead(
                from_key=from_key,
                to_key=to_key,
                condition_expression=condition,
                edge_order=order,
            )
            for order, (from_key, to_key, condition) in enumerate(edges)
        ]
    return detail


@router.get("/{workflow_id}", response_model=WorkflowDetailRead)
async def get_workflow(
    workflow_id: uuid.UUID,
    scope: Annotated[CompanyScope, Depends(company_scope)],
    service: Annotated[WorkflowService, Depends(get_workflow_service)],
) -> WorkflowDetailRead:
    return await _detail(service, workflow_id, scope)


@router.patch("/{workflow_id}", response_model=WorkflowDetailRead)
async def update_workflow(
    workflow_id: uuid.UUID,
    payload: WorkflowUpdate,
    scope: Annotated[CompanyScope, Depends(company_scope)],
    service: Annotated[WorkflowService, Depends(get_workflow_service)],
) -> WorkflowDetailRead:
    nodes, edges = _to_node_dicts(payload)
    await service.update_workflow(
        workflow_id,
        company_id=scope.company_id,
        name=payload.name,
        description=payload.description,
        nodes=nodes,
        edges=edges,
    )
    return await _detail(service, workflow_id, scope)


@router.delete("/{workflow_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_workflow(
    workflow_id: uuid.UUID,
    scope: Annotated[CompanyScope, Depends(company_scope)],
    service: Annotated[WorkflowService, Depends(get_workflow_service)],
) -> Response:
    await service.delete_workflow(workflow_id, company_id=scope.company_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{workflow_id}/validate", response_model=WorkflowValidationResponse)
async def validate_workflow(
    workflow_id: uuid.UUID,
    scope: Annotated[CompanyScope, Depends(company_scope)],
    service: Annotated[WorkflowService, Depends(get_workflow_service)],
) -> WorkflowValidationResponse:
    graph = await service.validate_workflow(workflow_id, company_id=scope.company_id)
    return WorkflowValidationResponse(
        valid=graph.is_valid,
        issues=[issue.as_dict() for issue in graph.issues],
    )


@router.post("/{workflow_id}/publish", response_model=WorkflowDetailRead)
async def publish_workflow(
    workflow_id: uuid.UUID,
    scope: Annotated[CompanyScope, Depends(company_scope)],
    service: Annotated[WorkflowService, Depends(get_workflow_service)],
) -> WorkflowDetailRead:
    try:
        await service.publish_workflow(workflow_id, company_id=scope.company_id)
    except WorkflowValidationError as exc:
        raise UnprocessableEntityError(
            "Workflow validation failed before publishing.",
            details=[issue.as_dict() for issue in exc.issues],
        ) from exc
    return await _detail(service, workflow_id, scope)


@router.post("/{workflow_id}/pause", response_model=WorkflowDetailRead)
async def pause_workflow(
    workflow_id: uuid.UUID,
    scope: Annotated[CompanyScope, Depends(company_scope)],
    service: Annotated[WorkflowService, Depends(get_workflow_service)],
) -> WorkflowDetailRead:
    await service.pause_workflow(workflow_id, company_id=scope.company_id)
    return WorkflowDetailRead.model_validate(await service.get_workflow(workflow_id, company_id=scope.company_id))


@router.post("/{workflow_id}/resume", response_model=WorkflowDetailRead)
async def resume_workflow(
    workflow_id: uuid.UUID,
    scope: Annotated[CompanyScope, Depends(company_scope)],
    service: Annotated[WorkflowService, Depends(get_workflow_service)],
) -> WorkflowDetailRead:
    await service.resume_workflow(workflow_id, company_id=scope.company_id)
    return WorkflowDetailRead.model_validate(await service.get_workflow(workflow_id, company_id=scope.company_id))


@router.post("/{workflow_id}/archive", response_model=WorkflowDetailRead)
async def archive_workflow(
    workflow_id: uuid.UUID,
    scope: Annotated[CompanyScope, Depends(company_scope)],
    service: Annotated[WorkflowService, Depends(get_workflow_service)],
) -> WorkflowDetailRead:
    await service.archive_workflow(workflow_id, company_id=scope.company_id)
    return WorkflowDetailRead.model_validate(await service.get_workflow(workflow_id, company_id=scope.company_id))


# --- executions ---------------------------------------------------------------------


@router.post("/{workflow_id}/execute", response_model=WorkflowExecutionRead, status_code=status.HTTP_202_ACCEPTED)
async def execute_workflow(
    workflow_id: uuid.UUID,
    payload: WorkflowExecuteRequest,
    scope: Annotated[CompanyScope, Depends(company_scope)],
    service: Annotated[WorkflowExecutionService, Depends(get_workflow_execution_service)],
) -> WorkflowExecutionRead:
    execution, _created = await service.execute_workflow(
        workflow_id,
        scope=scope,
        input_metadata=payload.input_metadata,
        idempotency_key=payload.idempotency_key,
    )
    return WorkflowExecutionRead.model_validate(execution)


# --- schedules -----------------------------------------------------------------------


@router.post("/{workflow_id}/schedules", response_model=ScheduleRead, status_code=status.HTTP_201_CREATED)
async def create_schedule(
    workflow_id: uuid.UUID,
    payload: ScheduleCreate,
    scope: Annotated[CompanyScope, Depends(company_scope)],
    service: Annotated[WorkflowExecutionService, Depends(get_workflow_execution_service)],
) -> ScheduleRead:
    from app.services.workflow_schedule_service import (  # noqa: PLC0415
        WorkflowScheduleMissedPolicy,
        WorkflowScheduleService,
        WorkflowScheduleType,
    )

    scheduler = WorkflowScheduleService(service.session, queue=service.queue)
    schedule = await scheduler.create_schedule(
        workflow_id,
        company_id=scope.company_id,
        name=payload.name,
        schedule_type=WorkflowScheduleType(payload.schedule_type),
        schedule_config=payload.schedule_config,
        timezone_name=payload.timezone,
        missed_policy=WorkflowScheduleMissedPolicy(payload.missed_policy),
        enabled=payload.enabled,
    )
    return ScheduleRead.model_validate(schedule)


@router.get("/{workflow_id}/schedules", response_model=ScheduleListResponse)
async def list_schedules(
    workflow_id: uuid.UUID,
    scope: Annotated[CompanyScope, Depends(company_scope)],
    service: Annotated[WorkflowService, Depends(get_workflow_service)],
) -> ScheduleListResponse:
    from app.services.workflow_schedule_service import WorkflowScheduleService  # noqa: PLC0415

    scheduler = WorkflowScheduleService(service.session, queue=None)
    schedules = await scheduler.list_schedules(workflow_id, company_id=scope.company_id)
    return ScheduleListResponse(
        items=[ScheduleRead.model_validate(schedule) for schedule in schedules],
        total=len(schedules),
    )
