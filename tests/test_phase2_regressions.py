"""Phase 2 regression tests: deadlocks and transaction boundaries found during
Phase 2 remain fixed. These run the exact scenarios that previously failed."""

from __future__ import annotations

import uuid

from sqlalchemy import select

from app.models import ConnectionStatus


async def test_activate_endpoint_twice_no_deadlock(client, company_and_platform):
    """Regression: activate used to open two UoWs on the same row and deadlock.

    It must now run the full validation cycle in a single unit of work and stay
    responsive for repeated activations.
    """
    _, platform = company_and_platform
    headers = {"X-Company-ID": str(platform.company_id)}

    platform_resp = await client.post(
        "/api/v1/platforms",
        json={"name": f"reg-platform-{uuid.uuid4().hex[:6]}", "platform_type": "api_service"},
        headers=headers,
    )
    platform_id = platform_resp.json()["id"]
    # The platforms API attaches new platforms to the default company; act as
    # the company that actually owns the platform (returned in the response).
    headers = {"X-Company-ID": platform_resp.json()["company_id"]}
    connection = await client.post(
        "/api/v1/connections",
        json={"platform_id": platform_id, "name": "primary", "provider": "diagnostic",
              "configuration": {"expected_config_key": "value"}},
        headers=headers,
    )
    assert connection.status_code == 201, connection.text
    connection_id = connection.json()["id"]

    first = await client.post(f"/api/v1/connections/{connection_id}/activate", headers=headers)
    assert first.status_code == 200
    assert first.json()["status_after"] == "connected"

    # Second activation (connected -> validating -> connected) must also work.
    second = await client.post(f"/api/v1/connections/{connection_id}/activate", headers=headers)
    assert second.status_code == 200
    assert second.json()["status_after"] == "connected"


async def test_connection_update_returns_server_managed_columns(client, company_and_platform):
    """Regression: PATCH used to fail serializing updated_at after flush."""
    _, platform = company_and_platform
    headers = {"X-Company-ID": str(platform.company_id)}
    platform_resp = await client.post(
        "/api/v1/platforms",
        json={"name": f"reg2-{uuid.uuid4().hex[:6]}", "platform_type": "api_service"},
        headers=headers,
    )
    platform_id = platform_resp.json()["id"]
    headers = {"X-Company-ID": platform_resp.json()["company_id"]}
    connection = await client.post(
        "/api/v1/connections",
        json={"platform_id": platform_id, "name": "primary", "provider": "diagnostic",
              "configuration": {"expected_config_key": "v1"}},
        headers=headers,
    )
    connection_id = connection.json()["id"]

    patched = await client.patch(
        f"/api/v1/connections/{connection_id}",
        json={"configuration": {"expected_config_key": "v2"}},
        headers=headers,
    )
    assert patched.status_code == 200
    body = patched.json()
    assert body["configuration"] == {"expected_config_key": "v2"}
    assert body["updated_at"] is not None  # server-managed column serialized

    # Validation after a configuration change recovers to connected.
    tested = await client.post(f"/api/v1/connections/{connection_id}/test", headers=headers)
    assert tested.json()["status_after"] == "connected"


async def test_deactivate_after_activate_serializes_cleanly(client, company_and_platform):
    """Regression: transitions now refresh server columns before serialization."""
    _, platform = company_and_platform
    headers = {"X-Company-ID": str(platform.company_id)}
    platform_resp = await client.post(
        "/api/v1/platforms",
        json={"name": f"reg3-{uuid.uuid4().hex[:6]}", "platform_type": "api_service"},
        headers=headers,
    )
    platform_id = platform_resp.json()["id"]
    headers = {"X-Company-ID": platform_resp.json()["company_id"]}
    connection = await client.post(
        "/api/v1/connections",
        json={"platform_id": platform_id, "name": "primary", "provider": "diagnostic",
              "configuration": {"expected_config_key": "value"}},
        headers=headers,
    )
    assert connection.status_code == 201, connection.text
    connection_id = connection.json()["id"]

    await client.post(f"/api/v1/connections/{connection_id}/activate", headers=headers)
    deactivated = await client.post(f"/api/v1/connections/{connection_id}/deactivate", headers=headers)
    assert deactivated.status_code == 200
    assert deactivated.json()["status"] == ConnectionStatus.DISCONNECTED.value
    assert deactivated.json()["updated_at"] is not None


async def test_connection_events_have_stable_ordering(client, db_session, company_and_platform):
    """Regression: clock_timestamp() guarantees same-transaction event order."""
    _, platform = company_and_platform
    headers = {"X-Company-ID": str(platform.company_id)}
    platform_resp = await client.post(
        "/api/v1/platforms",
        json={"name": f"reg4-{uuid.uuid4().hex[:6]}", "platform_type": "api_service"},
        headers=headers,
    )
    platform_id = platform_resp.json()["id"]
    headers = {"X-Company-ID": platform_resp.json()["company_id"]}
    connection = await client.post(
        "/api/v1/connections",
        json={"platform_id": platform_id, "name": "primary", "provider": "diagnostic",
              "configuration": {"expected_config_key": "value"}},
        headers=headers,
    )
    assert connection.status_code == 201, connection.text
    connection_id = connection.json()["id"]
    await client.post(f"/api/v1/connections/{connection_id}/activate", headers=headers)

    from app.models import ConnectionEvent

    events = list(
        (
            await db_session.execute(
                select(ConnectionEvent)
                .where(ConnectionEvent.connection_id == uuid.UUID(connection_id))
                .order_by(ConnectionEvent.created_at)
            )
        ).scalars().all()
    )
    to_statuses = [event.to_status for event in events]
    # Strictly: created -> validating -> connected (no duplicate-status shuffles).
    assert to_statuses == ["configured", "validating", "connected"]
    timestamps = [event.created_at for event in events]
    assert timestamps == sorted(timestamps)
