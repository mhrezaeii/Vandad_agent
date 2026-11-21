"""Admin-configured source adapters and the automatic company-wide refresh."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.integrations.platform_discovery.types import DiscoveredPage
from app.services.platform_discovery_service import PlatformDiscoveryService
from app.services.platform_monitor_service import PlatformMonitorService


@pytest.fixture
def headers(company_and_platform) -> dict:
    company, _platform = company_and_platform
    return {"X-Company-ID": str(company.id)}


async def test_openapi_discovery_persists_a_structured_source(
    client, headers, db_session, company_and_platform, monkeypatch
):
    company, platform = company_and_platform

    async def fake_load(self, url, *, allow_private=False):
        return DiscoveredPage(
            url=url,
            title="Vandefence API",
            text='{"openapi":"3.1.0","paths":{}}',
            source_type="openapi",
            status_code=200,
            metadata={"openapi": True},
        )

    monkeypatch.setattr(
        "app.integrations.platform_discovery.documents.StructuredDocumentLoader.load",
        fake_load,
    )

    response = await client.post(
        f"/api/v1/platforms/{platform.id}/discover",
        headers=headers,
        json={
            "mode": "openapi",
            "source_url": "https://docs.example.test/openapi.json",
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["created"] == 1
    sources = await PlatformDiscoveryService(db_session).list_sources(
        company_id=company.id, platform_id=platform.id
    )
    assert sources[0][0].source_type == "openapi"


async def test_company_refresh_uses_deterministic_monitor(
    client, headers, company_and_platform, monkeypatch
):
    company, _platform = company_and_platform
    calls = []

    async def fake_monitor_company(self, company_id, *, connection_id=None):
        calls.append(company_id)
        return {
            "company_id": str(company_id),
            "status": "completed",
            "platforms": [],
            "refreshed_count": 0,
            "unchanged_count": 0,
        }

    monkeypatch.setattr(PlatformMonitorService, "monitor_company", fake_monitor_company)

    response = await client.post(
        f"/api/v1/content/companies/{company.id}/refresh", headers=headers
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "completed"
    assert calls == [company.id]


async def test_internal_source_url_is_read_from_platform_configuration(
    db_session, company_and_platform, monkeypatch
):
    _company, platform = company_and_platform
    platform.configuration = {
        "knowledge_sources": {"internal_url": "http://web:3000/"}
    }
    await db_session.commit()

    captured = {}

    async def fake_internal(self, base_url, *, max_pages=25):
        captured["url"] = base_url
        return SimpleNamespace(pages=[], warnings=[])

    monkeypatch.setattr(
        "app.integrations.platform_discovery.documents.InternalSourceCrawler.crawl",
        fake_internal,
    )

    result = await PlatformDiscoveryService(db_session).discover(platform, mode="internal")

    assert captured["url"] == "http://web:3000/"
    assert result["scanned"] == 0
