"""Company-scoped API for agent task requests and run history."""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from app.agents import AgentRegistry
from app.api.deps import company_scope, get_agent_task_service
from app.models import AgentTask, AgentTaskEvent
from app.schemas.agent_task import (
    AgentTaskCreate,
    AgentTaskEventRead,
    AgentTaskListRead,
    AgentTaskRead,
    AgentTaskRunRead,
)
from app.security.authorization import CompanyScope
from app.services.agent_task_service import AgentTaskService

router = APIRouter(prefix="/agent-tasks", tags=["agent-tasks"])


def _task_read(task, registry: AgentRegistry) -> AgentTaskRead:
    return AgentTaskRead(
        id=task.id,
        company_id=task.company_id,
        agent_key=task.agent_key,
        capability=task.capability,
        title=task.title,
        status=task.status,
        priority=task.priority,
        input_payload=task.input_payload,
        result_payload=task.result_payload,
        idempotency_key=task.idempotency_key,
        requested_by=task.requested_by,
        cancel_requested=task.cancel_requested,
        attempt_count=task.attempt_count,
        max_attempts=task.max_attempts,
        error_code=task.error_code,
        error_message=task.error_message,
        started_at=task.started_at,
        completed_at=task.completed_at,
        created_at=task.created_at,
        updated_at=task.updated_at,
        agent_dispatch_ready=registry.get(task.agent_key).dispatch_ready,
    )


@router.post("", response_model=AgentTaskRead, status_code=status.HTTP_202_ACCEPTED)
async def create_agent_task(
    payload: AgentTaskCreate,
    scope: Annotated[CompanyScope, Depends(company_scope)],
    service: Annotated[AgentTaskService, Depends(get_agent_task_service)],
) -> AgentTaskRead:
    task = await service.create_task(
        scope.company_id,
        requested_by=scope.principal.subject,
        agent_key=payload.agent_key,
        capability=payload.capability,
        title=payload.title,
        input_payload=payload.input_payload,
        idempotency_key=payload.idempotency_key,
        priority=payload.priority,
        max_attempts=payload.max_attempts,
    )
    return _task_read(task, service.registry)


@router.get("", response_model=AgentTaskListRead)
async def list_agent_tasks(
    scope: Annotated[CompanyScope, Depends(company_scope)],
    service: Annotated[AgentTaskService, Depends(get_agent_task_service)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
    task_status: Literal[
        "pending", "queued", "awaiting_approval", "running", "completed", "failed", "cancelled"
    ] | None = Query(default=None, alias="status"),
    agent_key: Annotated[str | None, Query(max_length=80)] = None,
) -> AgentTaskListRead:
    rows, total = await service.list_tasks(
        scope.company_id, limit=limit, offset=offset, status=task_status, agent_key=agent_key
    )
    return AgentTaskListRead(
        items=[_task_read(row, service.registry) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{task_id}", response_model=AgentTaskRead)
async def get_agent_task(
    task_id: uuid.UUID,
    scope: Annotated[CompanyScope, Depends(company_scope)],
    service: Annotated[AgentTaskService, Depends(get_agent_task_service)],
) -> AgentTaskRead:
    task = await service.get_task(scope.company_id, task_id)
    return _task_read(task, service.registry)


@router.get("/{task_id}/runs", response_model=list[AgentTaskRunRead])
async def list_agent_task_runs(
    task_id: uuid.UUID,
    scope: Annotated[CompanyScope, Depends(company_scope)],
    service: Annotated[AgentTaskService, Depends(get_agent_task_service)],
) -> list[AgentTaskRunRead]:
    return await service.list_runs(scope.company_id, task_id)


@router.get("/{task_id}/events", response_model=list[AgentTaskEventRead])
async def list_agent_task_events(
    task_id: uuid.UUID,
    scope: Annotated[CompanyScope, Depends(company_scope)],
    service: Annotated[AgentTaskService, Depends(get_agent_task_service)],
    after: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[AgentTaskEventRead]:
    events = await service.list_events(scope.company_id, task_id, after=after, limit=limit)
    return [AgentTaskEventRead.model_validate(event) for event in events]


@router.get("/{task_id}/events/stream")
async def stream_agent_task_events(
    task_id: uuid.UUID,
    request: Request,
    scope: Annotated[CompanyScope, Depends(company_scope)],
    after: Annotated[int, Query(ge=0)] = 0,
) -> StreamingResponse:
    """Stream durable task events as SSE; reconnect with the last event sequence."""
    company_id = scope.company_id
    session_factory = request.app.state.session_factory

    async def event_stream():
        cursor = after
        while not await request.is_disconnected():
            async with session_factory() as session:
                task = await session.scalar(
                    select(AgentTask).where(
                        AgentTask.id == task_id, AgentTask.company_id == company_id
                    )
                )
                if task is None:
                    yield "event: error\ndata: {\"code\":\"not_found\"}\n\n"
                    return
                events = list((await session.scalars(
                    select(AgentTaskEvent).where(
                        AgentTaskEvent.task_id == task_id,
                        AgentTaskEvent.company_id == company_id,
                        AgentTaskEvent.sequence > cursor,
                    ).order_by(AgentTaskEvent.sequence.asc()).limit(100)
                )).all())
                terminal = task.status in {"completed", "failed", "cancelled"}
            for event in events:
                cursor = event.sequence
                payload = {
                    "id": str(event.id),
                    "task_id": str(event.task_id),
                    "sequence": event.sequence,
                    "type": event.event_type,
                    "payload": event.payload,
                    "created_at": event.created_at.astimezone(UTC).isoformat(),
                }
                yield f"id: {cursor}\nevent: {event.event_type}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
            if terminal and not events:
                return
            if not events:
                yield ": keep-alive\n\n"
                await asyncio.sleep(1)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/{task_id}/approve", response_model=AgentTaskRead)
async def approve_agent_task(
    task_id: uuid.UUID,
    scope: Annotated[CompanyScope, Depends(company_scope)],
    service: Annotated[AgentTaskService, Depends(get_agent_task_service)],
) -> AgentTaskRead:
    task = await service.approve_task(
        scope.company_id, task_id, approved_by=scope.principal.subject
    )
    await service.session.refresh(task)
    return _task_read(task, service.registry)


@router.post("/{task_id}/cancel", response_model=AgentTaskRead)
async def cancel_agent_task(
    task_id: uuid.UUID,
    scope: Annotated[CompanyScope, Depends(company_scope)],
    service: Annotated[AgentTaskService, Depends(get_agent_task_service)],
) -> AgentTaskRead:
    task = await service.cancel_task(
        scope.company_id, task_id, requested_by=scope.principal.subject
    )
    await service.session.refresh(task)
    return _task_read(task, service.registry)


@router.post("/{task_id}/retry", response_model=AgentTaskRead)
async def retry_agent_task(
    task_id: uuid.UUID,
    scope: Annotated[CompanyScope, Depends(company_scope)],
    service: Annotated[AgentTaskService, Depends(get_agent_task_service)],
) -> AgentTaskRead:
    task = await service.retry_task(
        scope.company_id, task_id, requested_by=scope.principal.subject
    )
    await service.session.refresh(task)
    return _task_read(task, service.registry)
