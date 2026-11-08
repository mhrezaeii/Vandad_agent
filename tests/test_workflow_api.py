"""Workflow API endpoint tests (CRUD, lifecycle, validation, schedules)."""

from __future__ import annotations

import uuid

VALID_GRAPH = {
    "nodes": [
        {"node_key": "start", "node_type": "trigger.manual", "name": "trigger"},
        {"node_key": "finish", "node_type": "control.complete", "name": "done",
         "configuration": {"outcome": "completed"}},
    ],
    "edges": [{"from_key": "start", "to_key": "finish"}],
}


def _payload(**overrides) -> dict:
    import copy

    payload = {"name": f"wf-{uuid.uuid4().hex[:8]}", **copy.deepcopy(VALID_GRAPH)}
    payload.update(overrides)
    return payload


async def test_create_and_get_workflow(client, company_and_platform):
    _, platform = company_and_platform
    headers = {"X-Company-ID": str(platform.company_id)}

    created = await client.post("/api/v1/workflows", json=_payload(), headers=headers)
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["status"] == "draft"
    assert body["company_id"] == str(platform.company_id)
    assert len(body["nodes"]) == 2
    assert len(body["edges"]) == 1

    got = await client.get(f"/api/v1/workflows/{body['id']}", headers=headers)
    assert got.status_code == 200
    assert got.json()["id"] == body["id"]


async def test_create_duplicate_name_409(client, company_and_platform):
    _, platform = company_and_platform
    headers = {"X-Company-ID": str(platform.company_id)}
    name = f"dup-{uuid.uuid4().hex[:6]}"
    first = await client.post("/api/v1/workflows", json=_payload(name=name), headers=headers)
    assert first.status_code == 201
    second = await client.post("/api/v1/workflows", json=_payload(name=name), headers=headers)
    assert second.status_code == 409


async def test_create_unknown_node_type_422(client, company_and_platform):
    _, platform = company_and_platform
    headers = {"X-Company-ID": str(platform.company_id)}
    payload = _payload()
    payload["nodes"][0]["node_type"] = "trigger.explosion"
    response = await client.post("/api/v1/workflows", json=payload, headers=headers)
    assert response.status_code == 422


async def test_list_workflows_tenant_scoped(client, db_session, company_and_platform):
    from app.models import Company

    _, platform = company_and_platform
    headers = {"X-Company-ID": str(platform.company_id)}
    await client.post("/api/v1/workflows", json=_payload(), headers=headers)

    other = Company(name="Wf Rival", slug="wf-rival")
    db_session.add(other)
    await db_session.commit()
    foreign = await client.get("/api/v1/workflows", headers={"X-Company-ID": str(other.id)})
    assert foreign.json()["total"] == 0
    mine = await client.get("/api/v1/workflows", headers=headers)
    assert mine.json()["total"] == 1


async def test_validate_reports_cycle_and_unreachable(client, company_and_platform):
    _, platform = company_and_platform
    headers = {"X-Company-ID": str(platform.company_id)}
    payload = _payload(
        nodes=[
            {"node_key": "start", "node_type": "trigger.manual", "name": "t"},
            {"node_key": "a", "node_type": "data.transform", "name": "a", "configuration": {"mappings": {"x": 1}}},
            {"node_key": "b", "node_type": "data.transform", "name": "b", "configuration": {"mappings": {"y": 2}}},
            {"node_key": "orphan", "node_type": "data.transform", "name": "o", "configuration": {"mappings": {"z": 3}}},
        ],
        edges=[
            {"from_key": "start", "to_key": "a"},
            {"from_key": "a", "to_key": "b"},
            {"from_key": "b", "to_key": "a"},  # cycle
            # orphan is unreachable
        ],
    )
    created = await client.post("/api/v1/workflows", json=payload, headers=headers)
    wid = created.json()["id"]

    validation = await client.post(f"/api/v1/workflows/{wid}/validate", headers=headers)
    assert validation.status_code == 200
    body = validation.json()
    assert body["valid"] is False
    codes = {issue["code"] for issue in body["issues"]}
    assert "cyclic_dependency" in codes
    assert "unreachable_node" in codes

    publish = await client.post(f"/api/v1/workflows/{wid}/publish", headers=headers)
    assert publish.status_code == 422  # cannot publish an invalid workflow


async def test_publish_pause_resume_archive_lifecycle(client, company_and_platform):
    _, platform = company_and_platform
    headers = {"X-Company-ID": str(platform.company_id)}
    created = await client.post("/api/v1/workflows", json=_payload(), headers=headers)
    assert created.status_code == 201, created.text
    wid = created.json()["id"]

    published = await client.post(f"/api/v1/workflows/{wid}/publish", headers=headers)
    assert published.status_code == 200
    assert published.json()["status"] == "published"

    paused = await client.post(f"/api/v1/workflows/{wid}/pause", headers=headers)
    assert paused.json()["status"] == "paused"

    resumed = await client.post(f"/api/v1/workflows/{wid}/resume", headers=headers)
    assert resumed.json()["status"] == "published"

    archived = await client.post(f"/api/v1/workflows/{wid}/archive", headers=headers)
    assert archived.json()["status"] == "archived"

    # Archived workflows cannot be modified.
    patch = await client.patch(
        f"/api/v1/workflows/{wid}", json={"description": "x"}, headers=headers
    )
    assert patch.status_code == 409
    execute = await client.post(
        f"/api/v1/workflows/{wid}/execute", json={}, headers=headers
    )
    assert execute.status_code == 409


async def test_invalid_transition_rejected(client, company_and_platform):
    _, platform = company_and_platform
    headers = {"X-Company-ID": str(platform.company_id)}
    created = await client.post("/api/v1/workflows", json=_payload(), headers=headers)
    assert created.status_code == 201, created.text
    wid = created.json()["id"]
    paused = await client.post(f"/api/v1/workflows/{wid}/pause", headers=headers)
    assert paused.status_code == 409  # draft -> paused is invalid


async def test_update_creates_new_draft_version(client, company_and_platform):
    _, platform = company_and_platform
    headers = {"X-Company-ID": str(platform.company_id)}
    created = await client.post("/api/v1/workflows", json=_payload(), headers=headers)
    assert created.status_code == 201, created.text
    wid = created.json()["id"]
    v1 = created.json()["current_version_id"]

    updated = await client.patch(
        f"/api/v1/workflows/{wid}",
        json={"nodes": VALID_GRAPH["nodes"] + [
            {"node_key": "extra", "node_type": "data.transform", "name": "e",
             "configuration": {"mappings": {"k": "v"}}}
        ], "edges": VALID_GRAPH["edges"] + [{"from_key": "start", "to_key": "extra"}]},
        headers=headers,
    )
    assert updated.status_code == 200
    # The published snapshot is immutable until re-publish: current_version_id
    # still points at v1, but the detail now shows the new draft graph.
    assert updated.json()["current_version_id"] == v1
    assert len(updated.json()["nodes"]) == 3

    republished = await client.post(f"/api/v1/workflows/{wid}/publish", headers=headers)
    assert republished.status_code == 200
    assert republished.json()["current_version_id"] != v1


async def test_delete_workflow(client, company_and_platform):
    _, platform = company_and_platform
    headers = {"X-Company-ID": str(platform.company_id)}
    created = await client.post("/api/v1/workflows", json=_payload(), headers=headers)
    assert created.status_code == 201, created.text
    wid = created.json()["id"]
    deleted = await client.delete(f"/api/v1/workflows/{wid}", headers=headers)
    assert deleted.status_code == 204
    gone = await client.get(f"/api/v1/workflows/{wid}", headers=headers)
    assert gone.status_code == 404


async def test_cross_company_workflow_access_404(client, db_session, company_and_platform):
    from app.models import Company

    _, platform = company_and_platform
    headers = {"X-Company-ID": str(platform.company_id)}
    created = await client.post("/api/v1/workflows", json=_payload(), headers=headers)
    assert created.status_code == 201, created.text
    wid = created.json()["id"]

    other = Company(name="Wf Sneak", slug="wf-sneak")
    db_session.add(other)
    await db_session.commit()
    response = await client.get(f"/api/v1/workflows/{wid}", headers={"X-Company-ID": str(other.id)})
    assert response.status_code == 404


async def test_execute_returns_202_and_enqueues(client, company_and_platform):
    _, platform = company_and_platform
    headers = {"X-Company-ID": str(platform.company_id)}
    created = await client.post("/api/v1/workflows", json=_payload(), headers=headers)
    assert created.status_code == 201, created.text
    wid = created.json()["id"]
    await client.post(f"/api/v1/workflows/{wid}/publish", headers=headers)

    executed = await client.post(
        f"/api/v1/workflows/{wid}/execute",
        json={"input_metadata": {"hello": "world"}, "idempotency_key": "api-run-1"},
        headers=headers,
    )
    assert executed.status_code == 202
    body = executed.json()
    assert body["status"] == "queued"
    assert body["input_metadata"] == {"hello": "world"}

    # Idempotency through the API: same key returns the same execution.
    again = await client.post(
        f"/api/v1/workflows/{wid}/execute",
        json={"input_metadata": {}, "idempotency_key": "api-run-1"},
        headers=headers,
    )
    assert again.json()["id"] == body["id"]

    listed = await client.get(
        f"/api/v1/executions?workflow_id={wid}", headers=headers
    )
    assert listed.json()["total"] == 1


async def test_schedule_crud_and_validation(client, company_and_platform):
    _, platform = company_and_platform
    headers = {"X-Company-ID": str(platform.company_id)}
    created = await client.post("/api/v1/workflows", json=_payload(), headers=headers)
    assert created.status_code == 201, created.text
    wid = created.json()["id"]
    await client.post(f"/api/v1/workflows/{wid}/publish", headers=headers)

    made = await client.post(
        f"/api/v1/workflows/{wid}/schedules",
        json={"name": "hourly", "schedule_type": "interval",
              "schedule_config": {"interval_seconds": 3600}},
        headers=headers,
    )
    assert made.status_code == 201, made.text
    schedule = made.json()
    assert schedule["next_run_at"] is not None
    assert schedule["enabled"] is True

    # Duplicate schedule name rejected.
    dup = await client.post(
        f"/api/v1/workflows/{wid}/schedules",
        json={"name": "hourly", "schedule_type": "interval",
              "schedule_config": {"interval_seconds": 60}},
        headers=headers,
    )
    assert dup.status_code == 409

    # Invalid cron rejected.
    bad = await client.post(
        f"/api/v1/workflows/{wid}/schedules",
        json={"name": "broken", "schedule_type": "cron", "schedule_config": {"cron": "not a cron"}},
        headers=headers,
    )
    assert bad.status_code == 409

    listed = await client.get(f"/api/v1/workflows/{wid}/schedules", headers=headers)
    assert listed.json()["total"] == 1

    disabled = await client.patch(
        f"/api/v1/schedules/{schedule['id']}", json={"enabled": False}, headers=headers
    )
    assert disabled.status_code == 200
    assert disabled.json()["enabled"] is False

    removed = await client.delete(f"/api/v1/schedules/{schedule['id']}", headers=headers)
    assert removed.status_code == 204


async def test_execution_steps_and_logs_endpoints(client, company_and_platform):
    _, platform = company_and_platform
    headers = {"X-Company-ID": str(platform.company_id)}
    created = await client.post("/api/v1/workflows", json=_payload(), headers=headers)
    assert created.status_code == 201, created.text
    wid = created.json()["id"]
    await client.post(f"/api/v1/workflows/{wid}/publish", headers=headers)
    executed = await client.post(
        f"/api/v1/workflows/{wid}/execute", json={}, headers=headers
    )
    eid = executed.json()["id"]

    steps = await client.get(f"/api/v1/executions/{eid}/steps", headers=headers)
    assert steps.status_code == 200
    assert steps.json()["total"] == 0  # not yet processed by a worker

    logs = await client.get(f"/api/v1/executions/{eid}/logs", headers=headers)
    assert logs.status_code == 200
    assert logs.json()["total"] == 0

    cancel = await client.post(f"/api/v1/executions/{eid}/cancel", headers=headers)
    assert cancel.status_code == 200
    assert cancel.json()["status"] == "cancelled"  # queued -> cancelled directly
