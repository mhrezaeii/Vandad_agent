"""Persist public and authenticated browser snapshots with tenant isolation."""
from __future__ import annotations
import hashlib
import uuid
from datetime import UTC, datetime
from typing import Any
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from app.integrations.platform_discovery.browser import BrowserAutomationCrawler
from app.integrations.platform_discovery.public_sources import PublicSourceCrawler
from app.models import Platform, PlatformConnection, PlatformSourceDocument

class PlatformDiscoveryService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def discover(self, platform: Platform, *, mode="public", max_pages=25,
                       connection: PlatformConnection | None = None,
                       credentials: dict[str, str] | None = None) -> dict[str, Any]:
        if not platform.base_url:
            raise ValueError("Platform base_url must be configured before discovery.")
        if mode == "public":
            result = await PublicSourceCrawler().crawl(platform.base_url, max_pages=max_pages)
        elif mode == "browser":
            if connection is None or connection.platform_id != platform.id or connection.company_id != platform.company_id:
                raise ValueError("A connection belonging to this platform and company is required.")
            result = await BrowserAutomationCrawler().crawl(
                platform.base_url, max_pages=min(max_pages, 25),
                browser_config=connection.configuration.get("browser", {}), credentials=credentials)
        else:
            raise ValueError("mode must be either 'public' or 'browser'.")
        now = datetime.now(UTC)
        created = changed = unchanged = 0
        for page in result.pages:
            digest = hashlib.sha256(page.text.encode("utf-8")).hexdigest()
            record = await self.session.scalar(select(PlatformSourceDocument).where(
                PlatformSourceDocument.company_id == platform.company_id,
                PlatformSourceDocument.platform_id == platform.id,
                PlatformSourceDocument.url == page.url))
            metadata = dict(page.metadata)
            metadata["source_type"] = page.source_type
            if record is None:
                self.session.add(PlatformSourceDocument(
                    company_id=platform.company_id, platform_id=platform.id, url=page.url,
                    source_type=page.source_type, title=page.title, content_text=page.text,
                    content_hash=digest, http_status=page.status_code, metadata_json=metadata,
                    last_seen_at=now, changed_at=now))
                created += 1
            else:
                if record.content_hash == digest:
                    unchanged += 1
                else:
                    changed += 1
                    record.content_text, record.content_hash, record.changed_at = page.text, digest, now
                record.source_type, record.title = page.source_type, page.title
                record.http_status, record.metadata_json, record.last_seen_at = page.status_code, metadata, now
        await self.session.flush()
        return {"platform_id": str(platform.id), "mode": mode, "scanned": len(result.pages),
                "created": created, "changed": changed, "unchanged": unchanged, "warnings": result.warnings}

    async def list_sources(self, *, company_id: uuid.UUID, platform_id: uuid.UUID,
                           limit=50, offset=0):
        query = select(PlatformSourceDocument).where(
            PlatformSourceDocument.company_id == company_id,
            PlatformSourceDocument.platform_id == platform_id)
        total = await self.session.scalar(select(func.count()).select_from(query.subquery()))
        rows = await self.session.scalars(query.order_by(
            PlatformSourceDocument.last_seen_at.desc()).limit(limit).offset(offset))
        return list(rows.all()), int(total or 0)
