"""Tests for the built-in agent catalog and company agent lifecycle API."""
from __future__ import annotations

import uuid

import pytest

from app.agents import AgentRegistry, UnknownAgentError, build_default_agent_registry
from app.agents.definitions import AgentDefinition
from app.models import Company


def test_default_catalog_has_unique_keys_and_capabilities():
    registry = build_default_agent_registry()
    definitions = registry.list_definitions()

    assert {item.key for item in definitions} == {
        "monitoring", "browser_identity", "content", "video"
    }
    assert registry.find_by_capability("content.scenario.generate")[0].key == "content"
    assert registry.find_by_capability("missing.capability") == ()
    assert all(item.dispatch_ready is False for item in definitions)


def test_registry_rejects_duplicate_keys():
    definition = AgentDefinition(
        key="same", name="One", description="One", capabilities=(), maturity="planned"
    )
    with pytest.raises(ValueError, match="unique"):
        AgentRegistry((definition, definition))


def test_registry_rejects_unknown_agent():
    with pytest.raises(UnknownAgentError):
        build_default_agent_registry().get("not-a-real-agent")


async def test_company_agent_provision_is_idempotent_and_disabled_by_default(client):
    first = await client.put("/api/v1/agents/monitoring")
    assert first.status_code == 200, first.text
    first_body = first.json()
    assert first_body["agent_key"] == "monitoring"
    assert first_body["status"] == "disabled"
    assert first_body["definition"]["dispatch_ready"] is False

    second = await client.put("/api/v1/agents/monitoring")
    assert second.status_code == 200, second.text
    assert second.json()["id"] == first_body["id"]

    listing = await client.get("/api/v1/agents")
    assert listing.status_code == 200
    assert [item["agent_key"] for item in listing.json()] == ["monitoring"]


async def test_company_agent_activation_requires_provisioning(client):
    missing = await client.patch(
        "/api/v1/agents/content/status", json={"status": "active"}
    )
    assert missing.status_code == 404

    await client.put("/api/v1/agents/content")
    activated = await client.patch(
        "/api/v1/agents/content/status", json={"status": "active"}
    )
    assert activated.status_code == 200, activated.text
    assert activated.json()["status"] == "active"

    disabled = await client.patch(
        "/api/v1/agents/content/status", json={"status": "disabled"}
    )
    assert disabled.status_code == 200
    assert disabled.json()["status"] == "disabled"


async def test_company_agent_catalog_and_unknown_key(client):
    catalog = await client.get("/api/v1/agents/catalog")
    assert catalog.status_code == 200
    assert len(catalog.json()["items"]) == 4

    unknown = await client.put("/api/v1/agents/not-approved")
    assert unknown.status_code == 404


async def test_company_agent_instances_are_isolated_by_company(client, db_session):
    first_company = Company(name="First", slug=f"first-{uuid.uuid4().hex[:8]}")
    second_company = Company(name="Second", slug=f"second-{uuid.uuid4().hex[:8]}")
    db_session.add_all([first_company, second_company])
    await db_session.commit()

    first_headers = {"X-Company-ID": str(first_company.id)}
    second_headers = {"X-Company-ID": str(second_company.id)}
    provisioned = await client.put("/api/v1/agents/monitoring", headers=first_headers)
    assert provisioned.status_code == 200, provisioned.text

    first_list = await client.get("/api/v1/agents", headers=first_headers)
    second_list = await client.get("/api/v1/agents", headers=second_headers)
    assert [item["agent_key"] for item in first_list.json()] == ["monitoring"]
    assert second_list.json() == []
