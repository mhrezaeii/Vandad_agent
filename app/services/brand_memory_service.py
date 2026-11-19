"""Brand memory: the knowledge and monitoring history accumulated for a brand.

The memory is not a second storage system. It composes the existing sources of
truth — ``BrandPlatformLink``, ``PlatformKnowledgeSnapshot`` and the
deterministic ``ContentAnalysisService`` — into one brand-scoped view, and it
refreshes that view through the existing ``PlatformMonitorService``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, NotFoundError
from app.models import (
    BrandPlatformLink,
    ContentBrand,
    Platform,
    PlatformKnowledgeSnapshot,
)
from app.services.brand_source_service import ensure_brand_source
from app.services.content_analysis_service import ContentAnalysisService
from app.services.platform_monitor_service import PlatformMonitorService

# Fact groups written by ``PlatformKnowledgeService``. Only these carry a
# ``source_url``; the free-text keys are exposed separately.
_FACT_GROUPS = (
    "products_and_services",
    "capabilities",
    "differentiators",
    "workflows",
    "target_audiences",
)
_MAX_HISTORY = 10
_MAX_EVIDENCE_CHARS = 600


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _snapshot_view(snapshot: PlatformKnowledgeSnapshot) -> dict[str, Any]:
    return {
        "id": snapshot.id,
        "extracted_at": _aware(snapshot.extracted_at),
        "source_document_count": len(snapshot.source_document_ids or []),
        "model": snapshot.model,
    }


def _knowledge_source_urls(knowledge: dict[str, Any]) -> set[str]:
    urls: set[str] = set()
    for group in _FACT_GROUPS:
        for fact in knowledge.get(group) or []:
            if isinstance(fact, dict):
                url = fact.get("source_url")
                if isinstance(url, str) and url.strip():
                    urls.add(url.strip())
    return urls


def _analysis_view(analysis: dict[str, Any]) -> dict[str, Any]:
    sources = []
    for item in (analysis.get("sources") or [])[:5]:
        if not isinstance(item, dict):
            continue
        sources.append(
            {
                "platform_id": item.get("platform_id"),
                "source_url": item.get("source_url"),
                "evidence": str(item.get("evidence") or "")[:_MAX_EVIDENCE_CHARS],
            }
        )
    supporting = []
    for item in (analysis.get("supporting_points") or [])[:6]:
        if not isinstance(item, dict):
            continue
        supporting.append(
            {
                "topic": item.get("topic"),
                "description": str(item.get("description") or "")[:_MAX_EVIDENCE_CHARS],
                "source_url": item.get("source_url"),
                "brand_fit": item.get("brand_fit"),
            }
        )
    return {
        "selected_topic": analysis.get("selected_topic"),
        "selection_reason": analysis.get("selection_reason"),
        "brand_fit": analysis.get("brand_fit"),
        "candidate_count": analysis.get("candidate_count"),
        "duplicate_count": analysis.get("duplicate_count"),
        "source_count": analysis.get("source_count"),
        "platform_count": analysis.get("platform_count"),
        "sources": sources,
        "supporting_points": supporting,
    }


async def brand_knowledge_freshness(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    brand_id: uuid.UUID,
    max_age_hours: int,
) -> dict[str, Any]:
    """Report whether every active linked platform has a recent snapshot.

    Production reuses recent knowledge instead of re-crawling immediately after
    the operator just triggered a memory refresh on the same page.
    """
    platform_rows = list(
        (
            await session.execute(
                select(Platform.id, Platform.status)
                .join(BrandPlatformLink, BrandPlatformLink.platform_id == Platform.id)
                .where(
                    BrandPlatformLink.company_id == company_id,
                    BrandPlatformLink.brand_id == brand_id,
                    Platform.company_id == company_id,
                )
            )
        ).all()
    )
    active_ids = [row.id for row in platform_rows if row.status == "active"]
    result: dict[str, Any] = {
        "fresh": False,
        "platform_count": len(active_ids),
        "snapshot_count": 0,
        "latest_extracted_at": None,
        "max_age_hours": max_age_hours,
    }
    if not active_ids:
        return result

    snapshots = list(
        (
            await session.scalars(
                select(PlatformKnowledgeSnapshot)
                .where(
                    PlatformKnowledgeSnapshot.company_id == company_id,
                    PlatformKnowledgeSnapshot.platform_id.in_(active_ids),
                )
                .order_by(PlatformKnowledgeSnapshot.extracted_at.desc())
            )
        ).all()
    )
    latest: dict[uuid.UUID, PlatformKnowledgeSnapshot] = {}
    for snapshot in snapshots:
        latest.setdefault(snapshot.platform_id, snapshot)
    result["snapshot_count"] = len(latest)
    if len(latest) != len(active_ids):
        return result

    extracted = [_aware(snapshot.extracted_at) for snapshot in latest.values()]
    result["latest_extracted_at"] = max(extracted)
    if max_age_hours <= 0:
        return result
    cutoff = datetime.now(UTC) - timedelta(hours=max_age_hours)
    result["fresh"] = all(value >= cutoff for value in extracted)
    return result


class BrandMemoryService:
    """Read and refresh the knowledge accumulated for one brand."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        content_service=None,  # noqa: ANN001 - ContentService
        registry=None,  # noqa: ANN001 - ProviderRegistry
        executor=None,  # noqa: ANN001 - ProviderExecutor
        master_key_box=None,  # noqa: ANN001 - MasterKeyBox
    ) -> None:
        self.session = session
        self.content_service = content_service
        self.registry = registry
        self.executor = executor
        self.master_key_box = master_key_box

    # ------------------------------------------------------------------ read
    async def read(self, *, company_id: uuid.UUID, brand_id: uuid.UUID) -> dict[str, Any]:
        brand = await self.session.get(ContentBrand, brand_id)
        if brand is None or brand.company_id != company_id:
            raise NotFoundError("Content brand not found for this company.")

        platforms = await self._linked_platforms(company_id=company_id, brand_id=brand_id)
        snapshot_count = int(
            await self.session.scalar(
                select(func.count())
                .select_from(PlatformKnowledgeSnapshot)
                .where(
                    PlatformKnowledgeSnapshot.company_id == company_id,
                    PlatformKnowledgeSnapshot.platform_id.in_(
                        [platform.id for platform in platforms]
                    ),
                )
            )
            or 0
        )

        entries: list[dict[str, Any]] = []
        source_urls: set[str] = set()
        latest_extracted_at: datetime | None = None
        for platform in platforms:
            history = list(
                (
                    await self.session.scalars(
                        select(PlatformKnowledgeSnapshot)
                        .where(
                            PlatformKnowledgeSnapshot.company_id == company_id,
                            PlatformKnowledgeSnapshot.platform_id == platform.id,
                        )
                        .order_by(PlatformKnowledgeSnapshot.extracted_at.desc())
                        .limit(_MAX_HISTORY)
                    )
                ).all()
            )
            latest = history[0] if history else None
            knowledge = (latest.knowledge_json or {}) if latest is not None else None
            if latest is not None:
                extracted = _aware(latest.extracted_at)
                if latest_extracted_at is None or extracted > latest_extracted_at:
                    latest_extracted_at = extracted
                if knowledge:
                    source_urls |= _knowledge_source_urls(knowledge)
            entries.append(
                {
                    "id": platform.id,
                    "name": platform.name,
                    "base_url": platform.base_url,
                    "platform_type": platform.platform_type,
                    "status": platform.status,
                    "snapshot": _snapshot_view(latest) if latest is not None else None,
                    "knowledge": knowledge or None,
                    "history": [_snapshot_view(item) for item in history],
                }
            )

        analysis = await self._analysis(company_id=company_id, brand_id=brand_id)
        return {
            "brand_id": brand.id,
            "brand_name": brand.display_name or brand.name,
            "language": brand.language,
            "has_knowledge": any(entry["snapshot"] for entry in entries),
            "platform_count": len(entries),
            "source_count": len(source_urls),
            "snapshot_count": snapshot_count,
            "suggested_topic": (analysis or {}).get("selected_topic"),
            "analysis": analysis,
            "platforms": entries,
            "updated_at": latest_extracted_at,
        }

    # --------------------------------------------------------------- refresh
    async def refresh(
        self,
        *,
        company_id: uuid.UUID,
        brand_id: uuid.UUID,
        source_url: str | None = None,
        connection_id: uuid.UUID | None = None,
    ) -> dict[str, Any]:
        if (
            self.content_service is None
            or self.registry is None
            or self.executor is None
            or self.master_key_box is None
        ):
            raise RuntimeError("Brand memory refresh requires provider dependencies.")

        brand = await self.content_service.get_brand(brand_id, company_id=company_id)
        if brand.status != "active":
            raise ConflictError("The selected brand is not active.")
        if source_url:
            await ensure_brand_source(
                self.session, self.content_service, brand, source_url, company_id
            )
            await self.session.commit()

        monitor = await PlatformMonitorService(
            self.session,
            self.registry,
            self.executor,
            self.master_key_box,
        ).monitor_brand(
            brand_id=brand_id,
            company_id=company_id,
            connection_id=connection_id,
        )
        await self.session.commit()
        memory = await self.read(company_id=company_id, brand_id=brand_id)
        return {
            "status": monitor.get("status"),
            "monitor": monitor,
            "memory": memory,
        }

    # --------------------------------------------------------------- helpers
    async def _linked_platforms(
        self, *, company_id: uuid.UUID, brand_id: uuid.UUID
    ) -> list[Platform]:
        return list(
            (
                await self.session.scalars(
                    select(Platform)
                    .join(
                        BrandPlatformLink,
                        BrandPlatformLink.platform_id == Platform.id,
                    )
                    .where(
                        BrandPlatformLink.company_id == company_id,
                        BrandPlatformLink.brand_id == brand_id,
                        Platform.company_id == company_id,
                    )
                    .order_by(Platform.created_at)
                )
            ).all()
        )

    async def _analysis(
        self, *, company_id: uuid.UUID, brand_id: uuid.UUID
    ) -> dict[str, Any] | None:
        try:
            analysis = await ContentAnalysisService(self.session).select(
                company_id=company_id, brand_id=brand_id, limit=10
            )
        except (ConflictError, NotFoundError):
            return None
        return _analysis_view(analysis)
