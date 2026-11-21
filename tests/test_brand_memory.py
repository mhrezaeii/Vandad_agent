"""Brand-scoped memory: knowledge, monitoring history and refresh endpoint."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.models import (
    BrandPlatformLink,
    Company,
    ContentBrand,
    Platform,
    PlatformKnowledgeSnapshot,
)
from app.services.brand_memory_service import (
    BrandMemoryService,
    brand_knowledge_freshness,
)
from app.services.platform_monitor_service import PlatformMonitorService

_KNOWLEDGE = {
    "summary": "ونداد منابع برند را پایش کرده و سناریو تولید می کند.",
    "products_and_services": [
        {
            "name": "تولید سناریو",
            "description": "سناریو فارسی مبتنی بر شواهد",
            "evidence": "تولید سناریو",
            "source_url": "https://product.example.test/features",
            "confidence": 0.9,
        }
    ],
    "capabilities": [],
    "differentiators": [],
    "workflows": [],
    "target_audiences": [],
    "unknowns": [],
}


async def _seed_brand(db_session, company, platform, *, ages=(2,), link=True):
    brand = ContentBrand(
        company_id=company.id,
        name=f"memory-{uuid.uuid4().hex[:8]}",
        display_name="ونداد",
        description="سامانه پایش منابع و تولید محتوا",
        language="fa-IR",
        status="active",
    )
    db_session.add(brand)
    await db_session.flush()
    if link:
        db_session.add(
            BrandPlatformLink(
                company_id=company.id, brand_id=brand.id, platform_id=platform.id
            )
        )
    for index, hours in enumerate(ages):
        db_session.add(
            PlatformKnowledgeSnapshot(
                company_id=company.id,
                platform_id=platform.id,
                source_document_ids=[str(uuid.uuid4())],
                source_fingerprint=f"{index:064d}",
                extracted_at=datetime.now(UTC) - timedelta(hours=hours),
                knowledge_json=_KNOWLEDGE,
            )
        )
    await db_session.commit()
    return brand


@pytest.fixture
def headers(company_and_platform) -> dict:
    company, _platform = company_and_platform
    return {"X-Company-ID": str(company.id)}


async def test_memory_reads_latest_knowledge_and_history(
    db_session, company_and_platform
):
    company, platform = company_and_platform
    brand = await _seed_brand(db_session, company, platform, ages=(48, 2))

    memory = await BrandMemoryService(db_session).read(
        company_id=company.id, brand_id=brand.id
    )

    assert memory["brand_id"] == brand.id
    assert memory["has_knowledge"] is True
    assert memory["platform_count"] == 1
    assert memory["snapshot_count"] == 2
    assert memory["source_count"] == 1
    assert memory["analysis"]["selected_topic"]
    assert memory["analysis"]["sources"][0]["source_url"].startswith("https://")
    entry = memory["platforms"][0]
    assert entry["name"] == platform.name
    assert entry["snapshot"]["source_document_count"] == 1
    assert len(entry["history"]) == 2
    assert entry["history"][0]["extracted_at"] > entry["history"][1]["extracted_at"]
    assert entry["knowledge"]["summary"] == _KNOWLEDGE["summary"]


async def test_memory_is_empty_without_snapshots(db_session, company_and_platform):
    company, platform = company_and_platform
    brand = await _seed_brand(db_session, company, platform, ages=())

    memory = await BrandMemoryService(db_session).read(
        company_id=company.id, brand_id=brand.id
    )

    assert memory["has_knowledge"] is False
    assert memory["snapshot_count"] == 0
    assert memory["analysis"] is None


async def test_freshness_requires_a_recent_snapshot_for_every_platform(
    db_session, company_and_platform
):
    company, platform = company_and_platform
    fresh = await _seed_brand(db_session, company, platform, ages=(1,))

    result = await brand_knowledge_freshness(
        db_session, company_id=company.id, brand_id=fresh.id, max_age_hours=12
    )
    assert result["fresh"] is True
    assert result["snapshot_count"] == 1

    stale_platform = Platform(
        company_id=company.id,
        name="Stale Source",
        slug=f"stale-{uuid.uuid4().hex[:8]}",
        status="active",
    )
    db_session.add(stale_platform)
    await db_session.flush()
    stale = await _seed_brand(db_session, company, stale_platform, ages=(48,))
    result = await brand_knowledge_freshness(
        db_session, company_id=company.id, brand_id=stale.id, max_age_hours=12
    )
    assert result["fresh"] is False

    empty_platform = Platform(
        company_id=company.id,
        name="Empty Source",
        slug=f"empty-{uuid.uuid4().hex[:8]}",
        status="active",
    )
    db_session.add(empty_platform)
    await db_session.flush()
    empty = await _seed_brand(db_session, company, empty_platform, ages=())
    result = await brand_knowledge_freshness(
        db_session, company_id=company.id, brand_id=empty.id, max_age_hours=12
    )
    assert result["fresh"] is False


async def test_memory_api_returns_brand_knowledge(client, headers, db_session, company_and_platform):
    company, platform = company_and_platform
    brand = await _seed_brand(db_session, company, platform)

    response = await client.get(
        f"/api/v1/content/brands/{brand.id}/memory", headers=headers
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["brand_id"] == str(brand.id)
    assert body["has_knowledge"] is True
    assert body["platforms"][0]["knowledge"]["summary"]


async def test_memory_api_is_tenant_isolated(
    client, headers, db_session, company_and_platform
):
    company, platform = company_and_platform
    brand = await _seed_brand(db_session, company, platform)
    other = Company(name="Other", slug=f"other-{uuid.uuid4().hex[:8]}")
    db_session.add(other)
    await db_session.commit()

    response = await client.get(
        f"/api/v1/content/brands/{brand.id}/memory",
        headers={"X-Company-ID": str(other.id)},
    )
    assert response.status_code == 404


async def test_memory_refresh_returns_monitor_status_and_memory(
    client, headers, db_session, company_and_platform, monkeypatch
):
    company, platform = company_and_platform
    brand = await _seed_brand(db_session, company, platform)

    calls: list[uuid.UUID] = []

    async def fake_monitor(self, *, brand_id, company_id, connection_id=None):
        calls.append(brand_id)
        return {
            "status": "completed",
            "platforms": [{"platform": platform.slug, "status": "ok"}],
            "snapshot_available": True,
            "successful_platforms": 1,
        }

    monkeypatch.setattr(PlatformMonitorService, "monitor_brand", fake_monitor)

    response = await client.post(
        f"/api/v1/content/brands/{brand.id}/memory/refresh",
        headers=headers,
        json={},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "completed"
    assert body["memory"]["has_knowledge"] is True
    assert calls == [brand.id]


async def test_memory_refresh_requires_a_source_for_an_unlinked_brand(
    client, headers, db_session, company_and_platform
):
    company, platform = company_and_platform
    brand = await _seed_brand(db_session, company, platform, link=False)

    response = await client.post(
        f"/api/v1/content/brands/{brand.id}/memory/refresh",
        headers=headers,
        json={},
    )

    assert response.status_code == 409, response.text


async def test_memory_refresh_can_link_a_public_source(
    client, headers, db_session, company_and_platform, monkeypatch
):
    company, platform = company_and_platform
    brand = await _seed_brand(db_session, company, platform, link=False, ages=())
    monkeypatch.setattr(
        "app.services.brand_source_service.validate_public_url", lambda url: url
    )

    async def fake_monitor(self, *, brand_id, company_id, connection_id=None):
        return {
            "status": "completed",
            "platforms": [{"status": "ok"}],
            "snapshot_available": True,
        }

    monkeypatch.setattr(PlatformMonitorService, "monitor_brand", fake_monitor)

    response = await client.post(
        f"/api/v1/content/brands/{brand.id}/memory/refresh",
        headers=headers,
        json={"source_url": "https://public.example.test/"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["memory"]["platform_count"] == 1
    assert body["memory"]["platforms"][0]["base_url"] == "https://public.example.test/"
