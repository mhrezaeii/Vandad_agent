"""Scheduled platform monitoring: observation only, never content generation."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.integrations.platform_discovery import DiscoveredPage, DiscoveryResult
from app.models import Platform, PlatformKnowledgeSnapshot
from app.services.platform_knowledge_extractor import EXTRACTOR_VERSION
from app.services.platform_monitor_service import PlatformMonitorService
from tests.test_worker_hardening import _keybox


@pytest.fixture
def monitor(db_session, engine_pair):
    registry, executor = engine_pair
    return PlatformMonitorService(db_session, registry, executor, _keybox())


def _page(url: str, text: str) -> DiscoveredPage:
    return DiscoveredPage(
        url=url,
        title="صفحه تست",
        text=text,
        source_type="public_page",
        status_code=200,
        metadata={},
    )


async def _make_platform(db_session, company_id) -> Platform:
    platform = Platform(
        company_id=company_id,
        name="Vandefence",
        slug=f"vandefence-{uuid.uuid4().hex[:6]}",
        platform_type="website",
        base_url="https://www.vandefence.ir",
        status="active",
        integration_type="manual",
        configuration={},
    )
    db_session.add(platform)
    await db_session.commit()
    return platform


async def test_monitor_extracts_without_llm_connection(
    db_session, monitor, company_and_platform, monkeypatch
):
    """Monitoring is fully deterministic and never needs an OpenAI key."""
    company, _platform_row = company_and_platform
    platform = await _make_platform(db_session, company.id)

    async def fake_crawl(self, base_url, *, max_pages=25):
        return DiscoveryResult(
            pages=[_page("https://www.vandefence.ir/", "محتوای صفحه اصلی")],
            warnings=[],
        )

    monkeypatch.setattr(
        "app.integrations.platform_discovery.public_sources.PublicSourceCrawler.crawl",
        fake_crawl,
    )

    result = await monitor.monitor_platform(platform)
    assert result["status"] == "ok"
    assert result["snapshot_id"]


async def test_monitor_skips_unchanged_documents(
    db_session, monitor, company_and_platform, monkeypatch
):
    company, _platform_row = company_and_platform
    platform = await _make_platform(db_session, company.id)

    async def fake_crawl(self, base_url, *, max_pages=25):
        return DiscoveryResult(
            pages=[_page("https://www.vandefence.ir/", "محتوای صفحه اصلی")],
            warnings=[],
        )

    monkeypatch.setattr(
        "app.integrations.platform_discovery.public_sources.PublicSourceCrawler.crawl",
        fake_crawl,
    )

    # First pass discovers documents and extracts a knowledge snapshot.
    # Second pass sees no created/changed pages and skips re-extraction.
    first = await monitor.monitor_platform(platform)
    assert first["status"] == "ok"

    from app.models import PlatformKnowledgeSnapshot

    db_session.add(PlatformKnowledgeSnapshot(
        company_id=platform.company_id,
        platform_id=platform.id,
        source_document_ids=[],
        source_fingerprint="seed",
        knowledge_json={},
        model=EXTRACTOR_VERSION,
        extracted_at=datetime.now(UTC),
    ))
    await db_session.commit()

    second = await monitor.monitor_platform(platform)
    assert second["status"] == "skipped"
    assert second["detail"] == "documents unchanged"


async def test_legacy_llm_snapshot_is_migrated_without_source_changes(
    db_session, monitor, company_and_platform, monkeypatch
):
    """The first sweep after deployment replaces retired LLM snapshots."""
    company, _platform_row = company_and_platform
    platform = await _make_platform(db_session, company.id)

    async def fake_crawl(self, base_url, *, max_pages=25):
        return DiscoveryResult(pages=[], warnings=[])

    monkeypatch.setattr(
        "app.integrations.platform_discovery.public_sources.PublicSourceCrawler.crawl",
        fake_crawl,
    )
    db_session.add(PlatformKnowledgeSnapshot(
        company_id=company.id,
        platform_id=platform.id,
        source_document_ids=[],
        source_fingerprint="legacy",
        knowledge_json={"summary": "old"},
        model="gpt-6-luna",
        extracted_at=datetime.now(UTC),
    ))
    await db_session.commit()

    result = await monitor.monitor_platform(platform)

    assert result["status"] == "ok"
    assert result["detail"] == "legacy LLM snapshot migrated"
    latest = (await db_session.scalars(
        select(PlatformKnowledgeSnapshot)
        .where(PlatformKnowledgeSnapshot.platform_id == platform.id)
        .order_by(PlatformKnowledgeSnapshot.extracted_at.desc())
    )).first()
    assert latest.model == EXTRACTOR_VERSION
