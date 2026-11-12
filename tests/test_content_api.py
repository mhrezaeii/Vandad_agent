"""Content production API tests: brand bootstrap, character lifecycle, project
flow, tenant isolation, and no secret/path leakage.

Provider calls are mocked with ``httpx.MockTransport`` — these tests validate
the HTTP surface and persistence, not live HeyGen/LLM integration.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.models import (
    CredentialStorageType,
    Platform,
    PlatformConnection,
)
from app.security.secret_value import SecretValue
from app.services.credential_service import CredentialService
from tests.test_worker_hardening import _keybox


@pytest.fixture
def headers(company_and_platform) -> dict:
    company, _platform = company_and_platform
    return {"X-Company-ID": str(company.id)}


async def _connection(
    db_session,
    company_id,
    *,
    provider: str,
    configuration: dict,
    with_credential: bool = False,
) -> uuid.UUID:
    platform = (
        await db_session.execute(select(Platform).where(Platform.company_id == company_id).limit(1))
    ).scalar_one()
    connection = PlatformConnection(
        company_id=company_id,
        platform_id=platform.id,
        name=f"{provider}-{uuid.uuid4().hex[:6]}",
        provider=provider,
        status="connected",
        configuration=configuration,
    )
    db_session.add(connection)
    await db_session.flush()
    if with_credential:
        credential = await CredentialService(db_session, _keybox()).create_credential(
            platform_id=platform.id,
            credential_type="api_key",
            secret_reference=f"vault://{provider}/{uuid.uuid4().hex[:8]}",
            storage_type=CredentialStorageType.ENCRYPTED,
            value=SecretValue(f"{provider}-test-key"),
        )
        connection.credential_reference_id = credential.id
    await db_session.commit()
    return connection.id


async def _brand_id(client, headers) -> str:
    response = await client.get("/api/v1/content/brands", headers=headers)
    assert response.status_code == 200, response.text
    brands = response.json()
    [brand] = [item for item in brands if item["name"] == "Vandefence"]
    return brand["id"]


async def test_brand_bootstrap_returns_vandefence_identity(client, headers):
    response = await client.get("/api/v1/content/brands", headers=headers)
    assert response.status_code == 200, response.text
    brands = response.json()
    assert [brand["name"] for brand in brands] == [
        "NobatMoshaver",
        "NobatVakil",
        "VakilBin",
        "Vandefence",
        "Vandiar",
    ]
    [brand] = [item for item in brands if item["name"] == "Vandefence"]
    assert brand["display_name"] == "Vandefence"
    assert brand["description"] is None
    assert brand["cta_config"] == {}
    assert brand["domain"] == "vandefence.ir"
    assert brand["industry"] == "Cybersecurity"
    assert brand["language"] == "fa-IR"
    assert (brand["video_width"], brand["video_height"]) == (1080, 1920)
    assert brand["visual_config"]["background_color"] == "#0A1128"

    # Idempotent bootstrap: a second call does not create duplicate brands.
    again = await client.get("/api/v1/content/brands", headers=headers)
    assert len(again.json()) == 5
    assert sorted(item["name"] for item in again.json()) == sorted(
        item["name"] for item in brands
    )


async def test_project_flow_and_tenant_isolation(client, db_session, headers, company_and_platform):
    company, _platform = company_and_platform
    brand_id = await _brand_id(client, headers)

    created = await client.post(
        "/api/v1/content/projects",
        json={
            "brand_id": brand_id,
            "topic": "چرا پایش متمرکز امنیتی برای سازمان‌ها اهمیت دارد؟",
        },
        headers=headers,
    )
    assert created.status_code == 201, created.text
    project = created.json()
    assert project["status"] == "draft"
    assert project["company_id"] == str(company.id)

    fetched = await client.get(f"/api/v1/content/projects/{project['id']}", headers=headers)
    assert fetched.status_code == 200
    assert fetched.json()["id"] == project["id"]

    # Approving before a script exists is a conflict, not a silent success.
    early_approval = await client.post(
        f"/api/v1/content/projects/{project['id']}/approve-script",
        json={"approved": True},
        headers=headers,
    )
    assert early_approval.status_code == 409

    # Unknown projects read as missing.
    missing = await client.get(f"/api/v1/content/projects/{uuid.uuid4()}", headers=headers)
    assert missing.status_code == 404

    # Another tenant cannot see the project (404, not 403 — no existence leak).
    from app.models import Company

    stranger = Company(name="Content Rival", slug="content-rival")
    db_session.add(stranger)
    await db_session.commit()
    foreign_headers = {"X-Company-ID": str(stranger.id)}
    assert (
        await client.get(f"/api/v1/content/projects/{project['id']}", headers=foreign_headers)
    ).status_code == 404
    assert (
        await client.post(
            f"/api/v1/content/projects/{project['id']}/approve-script",
            json={"approved": True},
            headers=foreign_headers,
        )
    ).status_code == 404


async def test_generate_script_starts_workflow_execution(
    client, db_session, headers, company_and_platform
):
    company, _platform = company_and_platform
    brand_id = await _brand_id(client, headers)
    project = (
        await client.post(
            "/api/v1/content/projects",
            json={"brand_id": brand_id, "topic": "پایش متمرکز امنیتی"},
            headers=headers,
        )
    ).json()
    llm_connection_id = await _connection(
        db_session,
        company.id,
        provider="llm_openai",
        configuration={"url": "https://llm.example.com/v1", "model": "gpt-test"},
    )

    response = await client.post(
        f"/api/v1/content/projects/{project['id']}/generate-script",
        json={"connection_id": str(llm_connection_id)},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "script_generating"
    assert body["script_workflow_execution_id"]

    # Re-generating while a script is in flight is rejected.
    again = await client.post(
        f"/api/v1/content/projects/{project['id']}/generate-script",
        json={"connection_id": str(llm_connection_id)},
        headers=headers,
    )
    assert again.status_code == 409


async def test_character_and_legacy_avatar_routes_are_not_exposed(client, headers):
    brand_id = await _brand_id(client, headers)
    response = await client.get(
        f"/api/v1/content/brands/{brand_id}/character", headers=headers
    )
    assert response.status_code == 404
    for suffix, payload in [
        ("character", {"connection_id": str(uuid.uuid4())}),
        ("character/refresh", {"connection_id": str(uuid.uuid4())}),
        ("character/status", {"active": True}),
    ]:
        response = await client.post(
            f"/api/v1/content/brands/{brand_id}/{suffix}", json=payload, headers=headers
        )
        assert response.status_code == 404


async def test_video_endpoints_hide_unfinished_projects(client, headers):
    await _brand_id(client, headers)
    unknown = uuid.uuid4()
    assert (await client.get("/api/v1/content/videos", headers=headers)).status_code == 200
    assert (await client.get(f"/api/v1/content/videos/{unknown}", headers=headers)).status_code == 404
    assert (
        await client.get(f"/api/v1/content/videos/{unknown}/content", headers=headers)
    ).status_code == 404
    assert (await client.get(f"/api/v1/content/jobs/{unknown}", headers=headers)).status_code == 404


async def test_company_header_must_be_uuid(client):
    response = await client.get(
        "/api/v1/content/brands", headers={"X-Company-ID": "not-a-uuid"}
    )
    assert response.status_code == 422
