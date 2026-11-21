"""Scheduled platform monitoring: crawl public pages, refresh knowledge.

Runs periodically in the worker. It only *observes*: public pages and any
admin-configured internal/OpenAPI/document sources are crawled, source documents
are refreshed, and knowledge snapshots are re-extracted when documents changed.
Extraction is deterministic and makes no model call; scenario and video
production remain explicit, billable, operator-initiated requests.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.providers import ProviderExecutor, ProviderRegistry
from app.models import BrandPlatformLink, Platform, PlatformKnowledgeSnapshot
from app.security.crypto import MasterKeyBox
from app.services.platform_discovery_service import PlatformDiscoveryService
from app.services.platform_knowledge_extractor import EXTRACTOR_VERSION
from app.services.platform_knowledge_service import PlatformKnowledgeService

logger = logging.getLogger("app.platform_monitor")


def _merge_crawls(first: dict[str, Any], second: dict[str, Any]) -> dict[str, Any]:
    """Combine counters from several source adapters into one crawl report."""
    return {
        "platform_id": first.get("platform_id", second.get("platform_id")),
        "mode": "combined",
        "scanned": int(first.get("scanned") or 0) + int(second.get("scanned") or 0),
        "created": int(first.get("created") or 0) + int(second.get("created") or 0),
        "changed": int(first.get("changed") or 0) + int(second.get("changed") or 0),
        "unchanged": int(first.get("unchanged") or 0) + int(second.get("unchanged") or 0),
        "warnings": list(first.get("warnings") or []) + list(second.get("warnings") or []),
    }


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

    async def run_cycle(self, *, company_id: uuid.UUID | None = None) -> list[dict[str, Any]]:
        """Monitor every active platform once; one failure never stops the rest."""
        query = select(Platform.id).where(Platform.status == "active")
        if company_id is not None:
            query = query.where(Platform.company_id == company_id)
        platform_ids = list((await self.session.scalars(
            query.order_by(Platform.created_at)
        )).all())
        results: list[dict[str, Any]] = []
        for platform_id in platform_ids:
            platform = await self.session.get(Platform, platform_id)
            if platform is None:
                continue
            platform_slug = platform.slug
            try:
                result = await self.monitor_platform(platform)
                await self.session.commit()
                results.append(result)
            except Exception as exc:  # noqa: BLE001 - one platform must not stop the cycle
                await self.session.rollback()
                logger.warning(
                    "monitoring platform %s failed: %s", platform_slug, type(exc).__name__
                )
                results.append({
                    "platform_id": str(platform_id),
                    "platform": platform_slug,
                    "status": "error",
                    "detail": type(exc).__name__,
                })
        return results

    async def monitor_company(
        self, company_id: uuid.UUID, *, connection_id: uuid.UUID | None = None
    ) -> dict[str, Any]:
        """Refresh every active platform of one company (automatic content sweep)."""
        results = await self.run_cycle(company_id=company_id)
        snapshots = [
            item for item in results if item.get("status") == "ok"
        ]
        unchanged = [
            item for item in results
            if item.get("status") == "skipped" and item.get("detail") == "documents unchanged"
        ]
        if any(item.get("status") == "error" for item in results):
            status = "partial_failure"
        elif results:
            status = "completed"
        else:
            status = "no_active_platforms"
        return {
            "company_id": str(company_id),
            "status": status,
            "platforms": results,
            "refreshed_count": len(snapshots),
            "unchanged_count": len(unchanged),
        }

    async def monitor_platform(
        self, platform: Platform, *, connection_id: uuid.UUID | None = None
    ) -> dict[str, Any]:
        """Crawl one platform and refresh its knowledge snapshot when changed."""
        base = {
            "platform_id": str(platform.id),
            "platform": platform.slug,
        }
        discovery = PlatformDiscoveryService(self.session)
        sources = (platform.configuration or {}).get("knowledge_sources") or {}
        if not isinstance(sources, dict):
            sources = {}

        crawl: dict[str, Any] | None = None
        if platform.base_url:
            crawl = await discovery.discover(platform, mode="public")
        else:
            crawl = {
                "scanned": 0, "created": 0, "changed": 0, "unchanged": 0, "warnings": []
            }

        if sources.get("internal_url"):
            internal = await discovery.discover(platform, mode="internal")
            crawl = _merge_crawls(crawl, internal)
        if sources.get("docs_url"):
            mode = str(sources.get("docs_mode") or "openapi")
            if mode not in {"openapi", "document"}:
                mode = "openapi"
            document = await discovery.discover(platform, mode=mode)
            crawl = _merge_crawls(crawl, document)

        latest_snapshot = (await self.session.execute(
            select(PlatformKnowledgeSnapshot.id, PlatformKnowledgeSnapshot.model)
            .where(
                PlatformKnowledgeSnapshot.company_id == platform.company_id,
                PlatformKnowledgeSnapshot.platform_id == platform.id,
            )
            .order_by(PlatformKnowledgeSnapshot.extracted_at.desc())
            .limit(1)
        )).first()
        # A snapshot produced by the retired LLM extractor must be replaced on
        # the next sweep, even when no source page changed. This migrates every
        # platform to the deterministic producer without any model call.
        legacy_snapshot = (
            latest_snapshot is not None and latest_snapshot.model != EXTRACTOR_VERSION
        )
        if latest_snapshot is None or legacy_snapshot or crawl["created"] or crawl["changed"]:
            snapshot = await PlatformKnowledgeService(
                self.session, self.registry, self.executor, self._keys
            ).extract(platform=platform, connection_id=connection_id)
            return {
                **base,
                "status": "ok",
                "documents": crawl,
                "snapshot_id": str(snapshot.id),
                **({"detail": "legacy LLM snapshot migrated"} if legacy_snapshot else {}),
            }
        return {**base, "status": "skipped", "detail": "documents unchanged", "documents": crawl}

    async def monitor_brand(
        self,
        *,
        brand_id: uuid.UUID,
        company_id: uuid.UUID,
        connection_id: uuid.UUID | None = None,
    ) -> dict[str, Any]:
        """Refresh every active platform explicitly linked to one content brand."""
        platform_ids = list((await self.session.scalars(
            select(BrandPlatformLink.platform_id).where(
                BrandPlatformLink.company_id == company_id,
                BrandPlatformLink.brand_id == brand_id,
            )
        )).all())
        if not platform_ids:
            return {"status": "no_linked_platforms", "platforms": [], "snapshot_count": 0}

        resolved_connection_id = connection_id

        platform_refs = list((await self.session.execute(
            select(Platform.id, Platform.slug, Platform.status).where(
                Platform.company_id == company_id,
                Platform.id.in_(platform_ids),
            ).order_by(Platform.created_at)
        )).all())
        active_platforms = [item for item in platform_refs if item.status == "active"]
        if not active_platforms:
            return {"status": "no_active_platforms", "platforms": [], "snapshot_count": 0}

        results: list[dict[str, Any]] = []
        for platform_id, platform_slug, platform_status in platform_refs:
            if platform_status != "active":
                results.append({
                    "platform_id": str(platform_id),
                    "platform": platform_slug,
                    "status": "skipped",
                    "detail": "platform is not active",
                })
                continue
            platform = await self.session.get(Platform, platform_id)
            if platform is None:
                results.append({
                    "platform_id": str(platform_id),
                    "platform": platform_slug,
                    "status": "error",
                    "detail": "platform not found",
                })
                continue
            try:
                result = await self.monitor_platform(
                    platform, connection_id=resolved_connection_id
                )
                await self.session.commit()
                results.append(result)
            except Exception as exc:  # noqa: BLE001 - isolate platform failures
                await self.session.rollback()
                logger.warning(
                    "brand monitoring failed for platform %s: %s",
                    platform_slug,
                    type(exc).__name__,
                )
                results.append({
                    "platform_id": str(platform_id),
                    "platform": platform_slug,
                    "status": "error",
                    "detail": type(exc).__name__,
                })

        snapshot_id = await self.session.scalar(
            select(PlatformKnowledgeSnapshot.id).where(
                PlatformKnowledgeSnapshot.company_id == company_id,
                PlatformKnowledgeSnapshot.platform_id.in_(platform_ids),
            ).order_by(PlatformKnowledgeSnapshot.extracted_at.desc()).limit(1)
        )
        successful_platforms = sum(
            1 for item in results
            if item.get("status") == "ok"
            or (
                item.get("status") == "skipped"
                and item.get("detail") == "documents unchanged"
            )
        )
        if snapshot_id is None:
            status = "no_knowledge"
        elif successful_platforms != len(platform_refs):
            status = "partial_failure"
        else:
            status = "completed"
        return {
            "status": status,
            "platforms": results,
            "snapshot_available": snapshot_id is not None,
            "successful_platforms": successful_platforms,
        }

