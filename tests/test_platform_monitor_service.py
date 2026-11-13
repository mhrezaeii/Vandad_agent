"""Scheduled platform monitoring: observation only, never content generation."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.integrations.platform_discovery import DiscoveredPage, DiscoveryResult
from app.models import Platform
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


async def test_monitor_skips_when_no_llm_connection(
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

    result = await monitor.monitor_platform(platform)
    assert result["status"] == "skipped"
    assert "no active llm_openai connection" in result["detail"]


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

    # First pass discovers documents (no LLM connection → knowledge skipped,
    # but the documents persist). Second pass sees no created/changed pages.
    first = await monitor.monitor_platform(platform)
    assert first["status"] == "skipped"

    from app.models import PlatformKnowledgeSnapshot

    db_session.add(PlatformKnowledgeSnapshot(
        company_id=platform.company_id,
        platform_id=platform.id,
        source_document_ids=[],
        source_fingerprint="seed",
        knowledge_json={},
        extracted_at=datetime.now(UTC),
    ))
    await db_session.commit()

    second = await monitor.monitor_platform(platform)
    assert second["status"] == "skipped"
    assert second["detail"] == "documents unchanged"
