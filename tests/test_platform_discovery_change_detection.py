from sqlalchemy import select

from app.integrations.platform_discovery.types import (
    DiscoveredPage,
    DiscoveryResult,
)
from app.models import Platform, PlatformSourceDocument
from app.services.platform_discovery_service import PlatformDiscoveryService


async def test_discovery_detects_created_unchanged_and_changed_content(
    company_and_platform,
    session_factory,
    monkeypatch,
):
    company, platform_fixture = company_and_platform

    contents = [
        "Vandad Agent test content version one",
        "Vandad Agent test content version one",
        "Vandad Agent test content version two",
    ]
    call_index = 0

    async def fake_crawl(self, base_url, *, max_pages=25):
        nonlocal call_index
        content = contents[call_index]
        call_index += 1

        return DiscoveryResult(
            pages=[
                DiscoveredPage(
                    url="https://example.com/change-detection-test",
                    title="Change Detection Test",
                    text=content,
                    source_type="website",
                    status_code=200,
                    metadata={"test": True},
                )
            ],
            warnings=[],
        )

    from app.integrations.platform_discovery.public_sources import PublicSourceCrawler

    monkeypatch.setattr(PublicSourceCrawler, "crawl", fake_crawl)

    async with session_factory() as session:
        platform = await session.get(Platform, platform_fixture.id)
        assert platform is not None

        platform.base_url = "https://example.com"
        await session.commit()

        service = PlatformDiscoveryService(session)

        first = await service.discover(platform, mode="public", max_pages=1)
        await session.commit()

        assert first["created"] == 1
        assert first["changed"] == 0
        assert first["unchanged"] == 0

        record = await session.scalar(
            select(PlatformSourceDocument).where(
                PlatformSourceDocument.platform_id == platform.id,
                PlatformSourceDocument.url
                == "https://example.com/change-detection-test",
            )
        )
        assert record is not None

        first_hash = record.content_hash
        first_changed_at = record.changed_at

        second = await service.discover(platform, mode="public", max_pages=1)
        await session.commit()

        assert second["created"] == 0
        assert second["changed"] == 0
        assert second["unchanged"] == 1

        await session.refresh(record)
        assert record.content_hash == first_hash
        assert record.changed_at == first_changed_at

        third = await service.discover(platform, mode="public", max_pages=1)
        await session.commit()

        assert third["created"] == 0
        assert third["changed"] == 1
        assert third["unchanged"] == 0

        await session.refresh(record)
        assert record.content_hash != first_hash
        assert record.content_text == contents[2]
        assert record.changed_at > first_changed_at

async def test_empty_discovery_does_not_delete_existing_snapshot(
    company_and_platform,
    session_factory,
    monkeypatch,
):
    from app.integrations.platform_discovery.public_sources import PublicSourceCrawler
    from app.models import Platform, PlatformSourceDocument

    company, platform_fixture = company_and_platform
    responses = [
        DiscoveryResult(
            pages=[
                DiscoveredPage(
                    url="https://example.com/preserved-page",
                    title="Preserved Page",
                    text="Important existing content",
                    source_type="website",
                    status_code=200,
                )
            ],
            warnings=[],
        ),
        DiscoveryResult(
            pages=[],
            warnings=["Temporary discovery failure"],
        ),
    ]

    async def fake_crawl(self, base_url, *, max_pages=25):
        return responses.pop(0)

    monkeypatch.setattr(PublicSourceCrawler, "crawl", fake_crawl)

    async with session_factory() as session:
        platform = await session.get(Platform, platform_fixture.id)
        platform.base_url = "https://example.com"

        service = PlatformDiscoveryService(session)

        first = await service.discover(platform, mode="public", max_pages=1)
        await session.commit()

        assert first["created"] == 1

        second = await service.discover(platform, mode="public", max_pages=1)
        await session.commit()

        assert second["scanned"] == 0
        assert second["warnings"] == ["Temporary discovery failure"]

        record = await session.scalar(
            select(PlatformSourceDocument).where(
                PlatformSourceDocument.platform_id == platform.id,
                PlatformSourceDocument.url == "https://example.com/preserved-page",
            )
        )

        assert record is not None
        assert record.content_text == "Important existing content"

