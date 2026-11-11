"""Agent task request, retry, cancellation and run lifecycle coverage."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.agents import AgentRegistry, build_default_agent_registry
from app.agents.definitions import AgentDefinition
from app.core.exceptions import ConflictError
from app.models import AgentTask, AgentTaskOutbox, Company, CompanyAgent
from app.services.agent_task_service import AgentTaskService


def _task_payload(**overrides):
    payload = {
        "agent_key": "content",
        "capability": "content.scenario.generate",
        "title": "Draft a short product announcement",
        "input_payload": {
            "project_id": "00000000-0000-4000-8000-000000000001",
            "connection_id": "00000000-0000-4000-8000-000000000002",
        },
        "idempotency_key": "task-request-001",
        "priority": 50,
        "max_attempts": 3,
    }
    payload.update(overrides)
    return payload


async def _activate_agent(client, agent_key="content"):
    provisioned = await client.put(f"/api/v1/agents/{agent_key}")
    assert provisioned.status_code == 200, provisioned.text
    activated = await client.patch(
        f"/api/v1/agents/{agent_key}/status", json={"status": "active"}
    )
    assert activated.status_code == 200, activated.text


async def test_task_creation_requires_active_agent_and_is_idempotent(client):
    disabled = await client.post("/api/v1/agent-tasks", json=_task_payload())
    assert disabled.status_code == 409

    await _activate_agent(client)
    first = await client.post("/api/v1/agent-tasks", json=_task_payload())
    assert first.status_code == 202, first.text
    first_body = first.json()
    assert first_body["status"] == "queued"
    assert first_body["attempt_count"] == 0
    assert first_body["agent_dispatch_ready"] is True

    second = await client.post("/api/v1/agent-tasks", json=_task_payload())
    assert second.status_code == 202
    assert second.json()["id"] == first_body["id"]

    changed = await client.post(
        "/api/v1/agent-tasks",
        json=_task_payload(title="A different request"),
    )
    assert changed.status_code == 409


async def test_task_spend_ceiling_is_validated_and_persisted(client):
    await _activate_agent(client)
    key = f"spend-ceiling-{uuid.uuid4()}"
    created = await client.post(
        "/api/v1/agent-tasks",
        json=_task_payload(
            idempotency_key=key,
            spend_ceiling_usd="2.500000",
        ),
    )
    assert created.status_code == 202, created.text
    assert created.json()["spend_ceiling_usd"] == "2.500000"

    replay = await client.post(
        "/api/v1/agent-tasks",
        json=_task_payload(
            idempotency_key=key,
            spend_ceiling_usd="2.500000",
        ),
    )
    assert replay.status_code == 202, replay.text

    invalid = await client.post(
        "/api/v1/agent-tasks",
        json=_task_payload(
            idempotency_key=f"invalid-ceiling-{uuid.uuid4()}",
            spend_ceiling_usd="0",
        ),
    )
    assert invalid.status_code == 422


async def test_task_progress_events_are_persisted_and_company_scoped(client):
    await _activate_agent(client)
    created = await client.post("/api/v1/agent-tasks", json=_task_payload())
    task_id = created.json()["id"]

    initial = await client.get(f"/api/v1/agent-tasks/{task_id}/events")
    assert initial.status_code == 200
    assert [event["event_type"] for event in initial.json()] == ["task.created"]

    cancelled = await client.post(f"/api/v1/agent-tasks/{task_id}/cancel")
    assert cancelled.status_code == 200
    history = await client.get(f"/api/v1/agent-tasks/{task_id}/events")
    assert [event["event_type"] for event in history.json()] == [
        "task.created",
        "task.cancelled",
    ]

    stream = await client.get(f"/api/v1/agent-tasks/{task_id}/events/stream?after=0")
    assert stream.status_code == 200
    assert "text/event-stream" in stream.headers["content-type"]
    assert "event: task.created" in stream.text
    assert "event: task.cancelled" in stream.text

    resumed = await client.get(
        f"/api/v1/agent-tasks/{task_id}/events/stream",
        headers={"Last-Event-ID": "1"},
    )
    assert resumed.status_code == 200
    assert "event: task.created" not in resumed.text
    assert "event: task.cancelled" in resumed.text


async def test_completed_task_result_is_redacted_before_database_persistence(
    client, db_session
):
    await _activate_agent(client)
    created = await client.post(
        "/api/v1/agent-tasks",
        json=_task_payload(idempotency_key=f"result-redaction-{uuid.uuid4()}"),
    )
    assert created.status_code == 202, created.text
    task = await db_session.get(AgentTask, uuid.UUID(created.json()["id"]))
    service = AgentTaskService(db_session, build_default_agent_registry())
    run = await service.start_run(task.company_id, task.id, worker_id="redaction-test")
    await service.complete_run(
        task.company_id,
        task.id,
        run.id,
        worker_id="redaction-test",
        result_payload={
            "message": "api_key=do-not-persist",
            "nested": [{"access_token": "do-not-persist-either"}],
            "status": "completed",
        },
    )
    await db_session.commit()
    await db_session.refresh(task)

    assert task.result_payload["message"] != "api_key=do-not-persist"
    assert task.result_payload["nested"][0]["access_token"] == "***REDACTED***"
    assert task.result_payload["status"] == "completed"


async def test_progress_events_are_redacted_and_allowlisted(client, db_session):
    await _activate_agent(client)
    created = await client.post("/api/v1/agent-tasks", json=_task_payload(
        idempotency_key=f"events-{uuid.uuid4()}",
    ))
    assert created.status_code == 202, created.text
    task_id = uuid.UUID(created.json()["id"])
    task = await db_session.get(AgentTask, task_id)
    service = AgentTaskService(db_session, build_default_agent_registry())
    await service._record_event(
        task,
        "progress",
        {
            "stage": "provider.requested",
            "api_key": "do-not-store",
            "untrusted_extra": "must-be-dropped",
            "status": "running",
        },
    )
    await db_session.commit()

    response = await client.get(f"/api/v1/agent-tasks/{task_id}/events")
    assert response.status_code == 200
    event = response.json()[-1]
    assert event["payload"] == {"stage": "provider.requested", "status": "running"}


async def test_video_task_requires_approval_before_outbox_dispatch(client, db_session):
    await _activate_agent(client, "video")
    payload = _task_payload(
        agent_key="video",
        capability="video.scene.generate",
        title="Generate approved scene video",
        input_payload={
            "project_id": str(uuid.uuid4()),
            "provider_fallbacks": ["apimaster", "generic_http"],
        },
        idempotency_key=f"video-{uuid.uuid4()}",
    )
    created = await client.post("/api/v1/agent-tasks", json=payload)
    assert created.status_code == 202, created.text
    body = created.json()
    assert body["status"] == "awaiting_approval"
    assert body["input_payload"]["provider_fallbacks"] == ["apimaster", "generic_http"]

    outbox_before = await db_session.scalar(
        select(AgentTaskOutbox).where(AgentTaskOutbox.task_id == uuid.UUID(body["id"]))
    )
    assert outbox_before is None

    approved = await client.post(f"/api/v1/agent-tasks/{body['id']}/approve")
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "queued"

    outbox_after = await db_session.scalar(
        select(AgentTaskOutbox).where(AgentTaskOutbox.task_id == uuid.UUID(body["id"]))
    )
    assert outbox_after is not None
    assert outbox_after.status == "pending"


async def test_task_payload_rejects_secrets_and_oversized_content(client):
    await _activate_agent(client)
    secret = await client.post(
        "/api/v1/agent-tasks",
        json=_task_payload(
            idempotency_key=f"secret-{uuid.uuid4()}",
            input_payload={"topic": "launch", "nested": {"api_key": "must-not-be-stored"}},
        ),
    )
    assert secret.status_code == 422

    embedded_secret = await client.post(
        "/api/v1/agent-tasks",
        json=_task_payload(
            idempotency_key=f"embedded-secret-{uuid.uuid4()}",
            input_payload={
                "project_id": "00000000-0000-4000-8000-000000000001",
                "connection_id": "00000000-0000-4000-8000-000000000002",
                "note": "api_key=do-not-store",
            },
        ),
    )
    assert embedded_secret.status_code == 422

    oversized = await client.post(
        "/api/v1/agent-tasks",
        json=_task_payload(
            idempotency_key=f"large-{uuid.uuid4()}",
            input_payload={"topic": "x" * (65 * 1024)},
        ),
    )
    assert oversized.status_code == 422


async def test_task_rejects_unregistered_capability(client):
    await _activate_agent(client)
    response = await client.post(
        "/api/v1/agent-tasks",
        json=_task_payload(capability="browser.delete_everything"),
    )
    assert response.status_code == 422


async def test_task_list_runs_and_cancel_are_company_scoped(client):
    await _activate_agent(client)
    created = await client.post("/api/v1/agent-tasks", json=_task_payload())
    task_id = created.json()["id"]

    listing = await client.get("/api/v1/agent-tasks")
    assert listing.status_code == 200
    assert listing.json()["total"] == 1
    assert listing.json()["items"][0]["id"] == task_id

    runs = await client.get(f"/api/v1/agent-tasks/{task_id}/runs")
    assert runs.status_code == 200
    assert runs.json() == []

    cancelled = await client.post(f"/api/v1/agent-tasks/{task_id}/cancel")
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled"

    repeated = await client.post(f"/api/v1/agent-tasks/{task_id}/cancel")
    assert repeated.status_code == 200
    assert repeated.json()["status"] == "cancelled"


async def test_task_is_not_visible_to_another_company(client, db_session):
    first = Company(name="Task Owner", slug=f"task-owner-{uuid.uuid4().hex[:8]}")
    second = Company(name="Other Tenant", slug=f"other-tenant-{uuid.uuid4().hex[:8]}")
    db_session.add_all([first, second])
    await db_session.commit()

    first_headers = {"X-Company-ID": str(first.id)}
    second_headers = {"X-Company-ID": str(second.id)}
    provisioned = await client.put("/api/v1/agents/content", headers=first_headers)
    assert provisioned.status_code == 200
    activated = await client.patch(
        "/api/v1/agents/content/status",
        headers=first_headers,
        json={"status": "active"},
    )
    assert activated.status_code == 200

    created = await client.post(
        "/api/v1/agent-tasks", headers=first_headers, json=_task_payload()
    )
    assert created.status_code == 202, created.text
    task_id = created.json()["id"]

    hidden = await client.get(f"/api/v1/agent-tasks/{task_id}", headers=second_headers)
    assert hidden.status_code == 404


async def test_run_completion_is_fenced_to_the_owning_worker(db_session):
    company = Company(name="Run Fence", slug=f"run-fence-{uuid.uuid4().hex[:8]}")
    db_session.add(company)
    await db_session.flush()
    db_session.add(CompanyAgent(company_id=company.id, agent_key="content", status="active"))
    await db_session.flush()
    registry = AgentRegistry((
        AgentDefinition(
            key="content", name="Content Agent", description="Test worker fencing",
            capabilities=("content.copy.generate",), maturity="partial", dispatch_ready=True,
        ),
    ))
    service = AgentTaskService(db_session, registry)
    task = await service.create_task(
        company.id, requested_by="test-user", agent_key="content",
        capability="content.copy.generate", title="Worker-fenced task",
        input_payload={"topic": "fencing"}, idempotency_key=None, priority=50, max_attempts=2,
    )
    run = await service.start_run(company.id, task.id, worker_id="worker-current")
    with pytest.raises(ConflictError, match="ownership"):
        await service.complete_run(
            company.id, task.id, run.id,
            result_payload={"text": "must not be accepted"}, worker_id="worker-stale",
        )
    await db_session.refresh(task)
    await db_session.refresh(run)
    assert task.status == "running"
    assert run.status == "running"
    assert run.worker_id == "worker-current"


async def test_run_failure_retry_and_completion_lifecycle(db_session):
    company = Company(name="Run Owner", slug=f"run-owner-{uuid.uuid4().hex[:8]}")
    db_session.add(company)
    await db_session.flush()
    db_session.add(
        CompanyAgent(company_id=company.id, agent_key="content", status="active")
    )
    await db_session.flush()

    registry = AgentRegistry((
        AgentDefinition(
            key="content",
            name="Content Agent",
            description="Test dispatch-ready agent",
            capabilities=("content.copy.generate",),
            maturity="partial",
            dispatch_ready=True,
        ),
    ))
    service = AgentTaskService(db_session, registry)
    task = await service.create_task(
        company.id,
        requested_by="test-user",
        agent_key="content",
        capability="content.copy.generate",
        title="Test task",
        input_payload={"topic": "test"},
        idempotency_key=None,
        priority=50,
        max_attempts=2,
    )

    first_run = await service.start_run(company.id, task.id, worker_id="worker-1")
    assert first_run.attempt_number == 1
    failed = await service.fail_run(
        company.id, task.id, first_run.id,
        error_code="provider_timeout",
        error_message="Provider timed out.",
    )
    assert failed.status == "failed"

    queued = await service.retry_task(company.id, task.id, requested_by="test-user")
    assert queued.status == "queued"
    second_run = await service.start_run(company.id, task.id, worker_id="worker-2")
    assert second_run.attempt_number == 2

    completed = await service.complete_run(
        company.id, task.id, second_run.id,
        result_payload={"text": "سلام"},
    )
    assert completed.status == "completed"
    assert completed.result_payload == {"text": "سلام"}

    history = await service.list_runs(company.id, task.id)
    assert [run.status for run in history] == ["failed", "completed"]

    with pytest.raises(ConflictError):
        await service.retry_task(company.id, task.id, requested_by="test-user")
