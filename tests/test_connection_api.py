"""Connection and provider API endpoint tests (with tenant isolation).

Every connection request carries ``X-Company-ID`` for the company that owns
the fixture platform — the same header a real tenant would send.
"""

from __future__ import annotations

import uuid

from app.security.authorization import Principal
from tests.provider_mocks import ScriptedProvider


def company_headers(platform) -> dict:
    return {"X-Company-ID": str(platform.company_id)}


async def _create_platform(client, name="API Platform") -> dict:
    response = await client.post(
        "/api/v1/platforms", json={"name": name, "platform_type": "api_service"}
    )
    assert response.status_code == 201, response.text
    return response.json()


def _connection_payload(platform_id: uuid.UUID, **overrides) -> dict:
    payload = {
        "platform_id": str(platform_id),
        "name": "primary",
        "provider": "diagnostic",
        "configuration": {"expected_config_key": "value"},
    }
    payload.update(overrides)
    return payload


async def _create_connection(client, platform, **overrides) -> dict:
    response = await client.post(
        "/api/v1/connections",
        json=_connection_payload(str(platform.id), **overrides),
        headers=company_headers(platform),
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_create_connection_returns_201(client, company_and_platform):
    _, platform = company_and_platform
    body = await _create_connection(client, platform)
    assert body["status"] == "configured"
    assert body["provider"] == "diagnostic"
    assert body["configuration"] == {"expected_config_key": "value"}
    assert body["credential_reference_id"] is None
    assert body["company_id"] == str(platform.company_id)


async def test_create_connection_unknown_provider_404(client, company_and_platform):
    _, platform = company_and_platform
    response = await client.post(
        "/api/v1/connections",
        json=_connection_payload(str(platform.id), provider="missing-provider"),
        headers=company_headers(platform),
    )
    assert response.status_code == 404


async def test_create_duplicate_name_409(client, company_and_platform):
    _, platform = company_and_platform
    await _create_connection(client, platform)
    response = await client.post(
        "/api/v1/connections",
        json=_connection_payload(str(platform.id)),
        headers=company_headers(platform),
    )
    assert response.status_code == 409


async def test_list_filter_and_pagination(client, company_and_platform):
    _, platform = company_and_platform
    await _create_connection(client, platform, name="first")
    await _create_connection(client, platform, name="second", configuration={})  # stays draft

    listed = (
        await client.get("/api/v1/connections", headers=company_headers(platform))
    ).json()
    assert listed["total"] == 2

    drafts = (
        await client.get(
            "/api/v1/connections?status=draft", headers=company_headers(platform)
        )
    ).json()
    assert drafts["total"] == 1
    assert drafts["items"][0]["name"] == "second"

    paged = (
        await client.get(
            "/api/v1/connections?limit=1&offset=1", headers=company_headers(platform)
        )
    ).json()
    assert paged["total"] == 2
    assert len(paged["items"]) == 1

    by_platform = (
        await client.get(
            f"/api/v1/connections?platform_id={platform.id}", headers=company_headers(platform)
        )
    ).json()
    assert by_platform["total"] == 2

    # Without the company header the list resolves to the default company,
    # which owns none of these connections.
    anonymous = (await client.get("/api/v1/connections")).json()
    assert anonymous["total"] == 0


async def test_invalid_status_filter_422(client):
    response = await client.get("/api/v1/connections?status=nonsense")
    assert response.status_code == 422


async def test_get_update_delete_connection(client, company_and_platform):
    _, platform = company_and_platform
    created = await _create_connection(client, platform)

    got = await client.get(
        f"/api/v1/connections/{created['id']}", headers=company_headers(platform)
    )
    assert got.status_code == 200

    patched = await client.patch(
        f"/api/v1/connections/{created['id']}",
        json={"configuration": {"expected_config_key": "changed"}},
        headers=company_headers(platform),
    )
    assert patched.status_code == 200
    assert patched.json()["configuration"] == {"expected_config_key": "changed"}

    deleted = await client.delete(
        f"/api/v1/connections/{created['id']}", headers=company_headers(platform)
    )
    assert deleted.status_code == 204
    gone = await client.get(
        f"/api/v1/connections/{created['id']}", headers=company_headers(platform)
    )
    assert gone.status_code == 404


async def test_test_and_activate_endpoints(client, company_and_platform):
    _, platform = company_and_platform
    created = await _create_connection(client, platform, name="lifecycle-conn")

    tested = await client.post(
        f"/api/v1/connections/{created['id']}/test", headers=company_headers(platform)
    )
    assert tested.status_code == 200
    body = tested.json()
    assert body["success"] is True
    assert body["status_after"] == "connected"
    assert body["provider_id"] == "diagnostic"
    assert body["degraded"] is False

    activated = await client.post(
        f"/api/v1/connections/{created['id']}/activate", headers=company_headers(platform)
    )
    assert activated.status_code == 200
    assert activated.json()["status_after"] == "connected"

    deactivated = await client.post(
        f"/api/v1/connections/{created['id']}/deactivate", headers=company_headers(platform)
    )
    assert deactivated.status_code == 200
    assert deactivated.json()["status"] == "disconnected"


async def test_live_provider_health_probe(client, company_and_platform):
    _, platform = company_and_platform
    created = await _create_connection(client, platform, name="live-health-probe")
    response = await client.post(
        f"/api/v1/connections/{created['id']}/health",
        headers=company_headers(platform),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["provider_id"] == "diagnostic"
    assert body["success"] is True
    assert body["attempts"] >= 1


async def test_health_endpoint_returns_stored_fields(client, company_and_platform):
    _, platform = company_and_platform
    created = await _create_connection(client, platform)
    health = await client.get(
        f"/api/v1/connections/{created['id']}/health", headers=company_headers(platform)
    )
    assert health.status_code == 200
    body = health.json()
    assert body["last_connected_at"] is None
    assert body["consecutive_failures"] == 0


async def test_events_endpoint_lists_history(client, company_and_platform):
    _, platform = company_and_platform
    created = await _create_connection(client, platform, configuration={})
    await client.post(
        f"/api/v1/connections/{created['id']}/test", headers=company_headers(platform)
    )

    events = (
        await client.get(
            f"/api/v1/connections/{created['id']}/events", headers=company_headers(platform)
        )
    ).json()
    assert events["total"] >= 3  # created, validating, connected
    types = [event["event_type"] for event in events["items"]]
    assert "created" in types
    assert "test_result" in types


async def test_providers_endpoints(client):
    listed = await client.get("/api/v1/providers")
    assert listed.status_code == 200
    providers = listed.json()
    ids = {provider["provider_id"] for provider in providers}
    assert {"diagnostic", "http_endpoint"} <= ids

    detail = await client.get("/api/v1/providers/diagnostic")
    assert detail.status_code == 200
    capabilities = detail.json()["capabilities"]
    assert "connection_validation" in capabilities

    missing = await client.get("/api/v1/providers/does-not-exist")
    assert missing.status_code == 404


async def test_cross_company_connection_access_is_404(client, db_session, company_and_platform):
    """A second company must not see (or even learn of) the first company's connection."""
    from app.models import Company

    _, platform = company_and_platform
    created = await _create_connection(client, platform)

    other_company = Company(name="Rival Co", slug="rival-co")
    db_session.add(other_company)
    await db_session.commit()

    foreign = {"X-Company-ID": str(other_company.id)}

    for path in (
        f"/api/v1/connections/{created['id']}",
        f"/api/v1/connections/{created['id']}/health",
        f"/api/v1/connections/{created['id']}/events",
    ):
        response = await client.get(path, headers=foreign)
        assert response.status_code == 404, path

    for action in ("test", "health", "activate", "deactivate"):
        response = await client.post(f"/api/v1/connections/{created['id']}/{action}", headers=foreign)
        assert response.status_code == 404, action

    deleted = await client.delete(f"/api/v1/connections/{created['id']}", headers=foreign)
    assert deleted.status_code == 404

    ok = await client.get(
        f"/api/v1/connections/{created['id']}", headers=company_headers(platform)
    )
    assert ok.status_code == 200


async def test_unauthorized_company_context_rejected(client, company_and_platform):
    """When the principal is restricted, a foreign X-Company-ID is a 403."""
    _, platform = company_and_platform
    created = await _create_connection(client, platform)

    foreign = uuid.uuid4()
    restricted = Principal(subject="limited", company_ids=frozenset({str(platform.company_id)}))
    client._transport.app.state.principal = restricted  # noqa: SLF001 - test wiring

    response = await client.get(
        f"/api/v1/connections/{created['id']}", headers={"X-Company-ID": str(foreign)}
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


async def test_malformed_company_header_422(client):
    response = await client.get("/api/v1/connections", headers={"X-Company-ID": "not-a-uuid"})
    assert response.status_code == 422


async def test_connection_with_inline_encrypted_credential_full_flow(
    client, company_and_platform
):
    _, platform = company_and_platform
    secret = "inline-super-secret-token"

    created = await _create_connection(
        client,
        platform,
        credential={
            "credential_type": "api_key",
            "storage_type": "encrypted",
            "secret_reference": "vault://acme/inline-key",
            "value": secret,
        },
    )
    assert created["credential_reference_id"]

    # The secret must never appear in any response.
    assert secret not in created.__str__()

    fetched = await client.get(
        f"/api/v1/connections/{created['id']}", headers=company_headers(platform)
    )
    assert secret not in fetched.text

    # A scripted provider that captures its input lets us verify the resolved
    # value end-to-end without exposing it via the API.
    app = client._transport.app  # noqa: SLF001 - test wiring
    captured: list[str] = []

    class CapturingProvider(ScriptedProvider):
        provider_id = "capturing"

        async def validate_connection(self, context):
            captured.append(context.credential.reveal() if context.credential else "")
            return await super().validate_connection(context)

    app.state.provider_registry.register(CapturingProvider())
    payload = _connection_payload(
        str(platform.id),
        name="capturing-conn",
        provider="capturing",
        credential={
            "credential_type": "api_key",
            "storage_type": "encrypted",
            "secret_reference": "vault://acme/capture-key",
            "value": secret,
        },
    )
    response = await client.post("/api/v1/connections", json=payload, headers=company_headers(platform))
    assert response.status_code == 201
    test_result = await client.post(
        f"/api/v1/connections/{response.json()['id']}/test", headers=company_headers(platform)
    )
    assert test_result.status_code == 200
    assert test_result.json()["success"] is True
    assert captured == [secret]

    # ...but the secret never leaks into the API response.
    assert secret not in test_result.text


async def test_encrypted_credential_requires_value(client, company_and_platform):
    _, platform = company_and_platform
    response = await client.post(
        "/api/v1/connections",
        json=_connection_payload(
            str(platform.id),
            name="no-value",
            credential={
                "credential_type": "api_key",
                "storage_type": "encrypted",
                "secret_reference": "vault://acme/no-value",
            },
        ),
        headers=company_headers(platform),
    )
    assert response.status_code == 422


async def test_reference_credential_forbids_value(client, company_and_platform):
    _, platform = company_and_platform
    response = await client.post(
        "/api/v1/connections",
        json=_connection_payload(
            str(platform.id),
            name="ref-with-value",
            credential={
                "credential_type": "api_key",
                "storage_type": "reference",
                "secret_reference": "vault://acme/external",
                "value": "must-not-be-sent",
            },
        ),
        headers=company_headers(platform),
    )
    assert response.status_code == 422


async def test_correlation_id_reaches_connection_events(client, db_session, company_and_platform):
    from sqlalchemy import select

    from app.models import ConnectionEvent

    _, platform = company_and_platform
    correlation_id = "corr-api-events-1234567"
    # Create AND test inside the same correlation window so every event row
    # (including "created") carries the id.
    response = await client.post(
        "/api/v1/connections",
        json=_connection_payload(str(platform.id), name="corr-conn"),
        headers={**company_headers(platform), "X-Request-ID": correlation_id},
    )
    assert response.status_code == 201, response.text
    created = response.json()

    response = await client.post(
        f"/api/v1/connections/{created['id']}/test",
        headers={**company_headers(platform), "X-Request-ID": correlation_id},
    )
    assert response.status_code == 200

    events = (
        await db_session.execute(
            select(ConnectionEvent).where(ConnectionEvent.connection_id == uuid.UUID(created["id"]))
        )
    ).scalars().all()
    assert events
    assert all(event.correlation_id == correlation_id for event in events)
