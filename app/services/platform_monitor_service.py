"""Scheduled platform monitoring: crawl public pages, refresh knowledge.

Runs periodically in the worker. It only *observes*: public pages are crawled,
source documents are refreshed, and knowledge snapshots are re-extracted when
documents changed. It never generates content — scenario and video production
remain explicit, billable, operator-initiated requests. The only spend here is
the small LLM cost of re-extracting knowledge after real content changes.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.providers import ProviderExecutor, ProviderRegistry
from app.models import Platform, PlatformConnection, PlatformKnowledgeSnapshot
from app.security.crypto import MasterKeyBox
from app.services.platform_discovery_service import PlatformDiscoveryService
from app.services.platform_knowledge_service import PlatformKnowledgeService

logger = logging.getLogger("app.platform_monitor")


class PlatformMonitorService:
    def __init__(
        self,
        session: AsyncSession,
        registry: ProviderRegistry,
        executor: ProviderExecutor,
        master_key_box: MasterKeyBox,
    ) -> None:
        self.session = session
        self.registry = registry
        self.executor = executor
        self._keys = master_key_box

    async def run_cycle(self) -> list[dict[str, Any]]:
        """Monitor every active platform once; one failure never stops the rest."""
        platforms = list((await self.session.scalars(
            select(Platform).where(Platform.status == "active").order_by(Platform.created_at)
        )).all())
        results: list[dict[str, Any]] = []
        for platform in platforms:
            try:
                results.append(await self.monitor_platform(platform))
            except Exception as exc:  # noqa: BLE001 - one platform must not stop the cycle
                await self.session.rollback()
                logger.warning(
                    "monitoring platform %s failed: %s", platform.slug, type(exc).__name__
                )
                results.append({
                    "platform_id": str(platform.id),
                    "platform": platform.slug,
                    "status": "error",
                    "detail": type(exc).__name__,
                })
        return results

    async def monitor_platform(self, platform: Platform) -> dict[str, Any]:
        """Crawl one platform and refresh its knowledge snapshot when changed."""
        base = {
            "platform_id": str(platform.id),
            "platform": platform.slug,
        }
        if not platform.base_url:
            return {**base, "status": "skipped", "detail": "no base_url configured"}

        crawl = await PlatformDiscoveryService(self.session).discover(platform, mode="public")

        has_snapshot = (await self.session.scalar(
            select(PlatformKnowledgeSnapshot.id).where(
                PlatformKnowledgeSnapshot.company_id == platform.company_id,
                PlatformKnowledgeSnapshot.platform_id == platform.id,
            ).limit(1)
        )) is not None
        if not has_snapshot or crawl["created"] or crawl["changed"]:
            connection_id = await self._llm_connection_id(platform.company_id)
            if connection_id is None:
                await self.session.commit()
                return {**base, "status": "skipped", "detail": "no active llm_openai connection",
                        "documents": crawl}
            snapshot = await PlatformKnowledgeService(
                self.session, self.registry, self.executor, self._keys
            ).extract(platform=platform, connection_id=connection_id)
            await self.session.commit()
            return {
                **base,
                "status": "ok",
                "documents": crawl,
                "snapshot_id": str(snapshot.id),
            }
        await self.session.commit()
        return {**base, "status": "skipped", "detail": "documents unchanged", "documents": crawl}

    async def _llm_connection_id(self, company_id: uuid.UUID) -> uuid.UUID | None:
        return await self.session.scalar(
            select(PlatformConnection.id).where(
                PlatformConnection.company_id == company_id,
                PlatformConnection.provider == "llm_openai",
                PlatformConnection.status == "connected",
            ).order_by(PlatformConnection.created_at.desc()).limit(1)
        )
