"""Platform CRUD API tests, including validation and audit integration."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditEvent, Company, CredentialReference


async def _create_platform(client, payload: dict) -> dict:
    response = await client.post("/api/v1/platforms", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


async def test_create_platform_defaults_and_auto_slug(client, platform_payload):
    body = await _create_platform(client, platform_payload)

    assert body["name"] == "Admin Panel"
    assert body["slug"] == "admin-panel"
    assert body["platform_type"] == "admin_panel"
    assert body["status"] == "active"
    assert body["integration_type"] == "browser_automation"
    assert body["base_url"] == "https://admin.example.com"
    assert body["configuration"] == {"login_url": "https://admin.example.com/login"}
    assert uuid.UUID(body["id"])
    assert body["created_at"] and body["updated_at"]


async def test_create_platform_attaches_to_default_company(client, platform_payload):
    body = await _create_platform(client, platform_payload)
    assert body["company_id"]


async def test_create_platform_with_explicit_company(client, db_session: AsyncSession, platform_payload):
    company = Company(name="Acme Corp", slug="acme-corp")
    db_session.add(company)
    await db_session.commit()

    payload = {**platform_payload, "company_id": str(company.id), "slug": "acme-platform"}
    body = await _create_platform(client, payload)
    assert body["company_id"] == str(company.id)
    assert body["slug"] == "acme-platform"


async def test_create_platform_unknown_company_returns_404(client, platform_payload):
    payload = {**platform_payload, "company_id": str(uuid.uuid4())}
    response = await client.post("/api/v1/platforms", json=payload)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


async def test_create_duplicate_slug_returns_409(client, platform_payload):
    await _create_platform(client, platform_payload)
    response = await client.post("/api/v1/platforms", json=platform_payload)
    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "conflict"
    assert "slug" in body["error"]["message"]


async def test_same_slug_allowed_across_companies(client, db_session: AsyncSession, platform_payload):
    company_a = Company(name="Alpha", slug="alpha")
    company_b = Company(name="Beta", slug="beta")
    db_session.add_all([company_a, company_b])
    await db_session.commit()

    payload_a = {**platform_payload, "company_id": str(company_a.id), "slug": "shared-slug"}
    payload_b = {**platform_payload, "company_id": str(company_b.id), "slug": "shared-slug"}
    await _create_platform(client, payload_a)
    body_b = await _create_platform(client, payload_b)
    assert body_b["slug"] == "shared-slug"


async def test_get_platform(client, platform_payload):
    created = await _create_platform(client, platform_payload)
    response = await client.get(f"/api/v1/platforms/{created['id']}")
    assert response.status_code == 200
    assert response.json()["id"] == created["id"]


async def test_get_missing_platform_returns_404(client):
    response = await client.get(f"/api/v1/platforms/{uuid.uuid4()}")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


async def test_list_platforms_pagination_and_filters(client, platform_payload):
    await _create_platform(client, platform_payload)
    await _create_platform(client, {**platform_payload, "name": "Marketing Site", "status": "inactive"})
    await _create_platform(client, {**platform_payload, "name": "Mobile App", "platform_type": "mobile_app"})

    response = await client.get("/api/v1/platforms")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 3
    assert len(body["items"]) == 3

    paged = (await client.get("/api/v1/platforms?limit=2&offset=1")).json()
    assert paged["total"] == 3
    assert len(paged["items"]) == 2

    filtered = (await client.get("/api/v1/platforms?status_filter=inactive")).json()
    assert filtered["total"] == 1
    assert filtered["items"][0]["name"] == "Marketing Site"

    typed = (await client.get("/api/v1/platforms?platform_type=mobile_app")).json()
    assert typed["total"] == 1
    assert typed["items"][0]["name"] == "Mobile App"


async def test_update_platform_partial(client, platform_payload):
    created = await _create_platform(client, platform_payload)

    response = await client.patch(
        f"/api/v1/platforms/{created['id']}",
        json={"description": "Updated description", "status": "maintenance"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["description"] == "Updated description"
    assert body["status"] == "maintenance"
    # Untouched fields stay intact
    assert body["name"] == "Admin Panel"
    assert body["base_url"] == "https://admin.example.com"


async def test_update_platform_missing_returns_404(client):
    response = await client.patch(f"/api/v1/platforms/{uuid.uuid4()}", json={"status": "active"})
    assert response.status_code == 404


async def test_update_platform_slug_conflict_returns_409(client, platform_payload):
    first = await _create_platform(client, platform_payload)
    second = await _create_platform(client, {**platform_payload, "name": "Other Platform"})

    response = await client.patch(f"/api/v1/platforms/{second['id']}", json={"slug": first["slug"]})
    assert response.status_code == 409


async def test_update_platform_rejects_unknown_fields(client, platform_payload):
    created = await _create_platform(client, platform_payload)
    response = await client.patch(
        f"/api/v1/platforms/{created['id']}", json={"nonexistent_field": "value"}
    )
    assert response.status_code == 422


async def test_delete_platform(client, platform_payload):
    created = await _create_platform(client, platform_payload)

    response = await client.delete(f"/api/v1/platforms/{created['id']}")
    assert response.status_code == 204

    follow_up = await client.get(f"/api/v1/platforms/{created['id']}")
    assert follow_up.status_code == 404


async def test_delete_missing_platform_returns_404(client):
    response = await client.delete(f"/api/v1/platforms/{uuid.uuid4()}")
    assert response.status_code == 404


async def test_delete_platform_with_credentials_returns_409(
    client, db_session: AsyncSession, platform_payload
):
    created = await _create_platform(client, platform_payload)
    credential = CredentialReference(
        platform_id=uuid.UUID(created["id"]),
        credential_type="api_key",
        secret_reference="vault://admin-panel/api-key",
    )
    db_session.add(credential)
    await db_session.commit()

    response = await client.delete(f"/api/v1/platforms/{created['id']}")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conflict"


async def test_create_validation_errors(client, platform_payload):
    # Missing required name
    response = await client.post("/api/v1/platforms", json={"platform_type": "website"})
    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "validation_error"
    assert isinstance(body["error"]["details"], list) and body["error"]["details"]
    assert any("name" in detail["loc"] for detail in body["error"]["details"])

    # Invalid slug format
    response = await client.post("/api/v1/platforms", json={**platform_payload, "slug": "Not A Slug!"})
    assert response.status_code == 422

    # Invalid base_url scheme
    response = await client.post("/api/v1/platforms", json={**platform_payload, "base_url": "ftp://x.example.com"})
    assert response.status_code == 422

    # Invalid enum value
    response = await client.post("/api/v1/platforms", json={**platform_payload, "platform_type": "spaceship"})
    assert response.status_code == 422


async def test_audit_events_recorded_for_platform_mutations(
    client, db_session: AsyncSession, platform_payload
):
    created = await _create_platform(client, platform_payload)
    await client.patch(f"/api/v1/platforms/{created['id']}", json={"status": "inactive"})
    await client.delete(f"/api/v1/platforms/{created['id']}")

    result = await db_session.execute(
        select(AuditEvent).where(AuditEvent.resource_id == created["id"]).order_by(AuditEvent.created_at)
    )
    events = list(result.scalars().all())

    assert [event.event_type for event in events] == [
        "platform.created",
        "platform.updated",
        "platform.deleted",
    ]
    assert all(event.actor_type == "system" for event in events)
    assert all(event.resource_type == "platform" for event in events)
    assert events[0].event_metadata["slug"] == created["slug"]
    assert events[1].event_metadata["changed_fields"] == ["status"]
    # Deleted platform leaves audit trail with platform reference nulled out
    assert events[2].platform_id is None


async def test_audit_event_metadata_never_contains_secrets(
    client, db_session: AsyncSession, platform_payload
):
    payload = {
        **platform_payload,
        "configuration": {"api_key": "super-secret-value", "endpoint": "/v1/jobs"},
    }
    created = await _create_platform(client, payload)

    result = await db_session.execute(
        select(AuditEvent).where(AuditEvent.resource_id == created["id"])
    )
    event = result.scalar_one()
    assert event.event_metadata["platform_type"] == "admin_panel"
    assert "super-secret-value" not in str(event.event_metadata)
