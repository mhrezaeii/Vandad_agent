"""Generate a content scenario from a tenant's latest verified platform knowledge."""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, NotFoundError
from app.integrations.providers import ProviderExecutor, ProviderRegistry
from app.models import (
    BrandPlatformLink,
    ContentBrand,
    ContentProject,
    PlatformConnection,
    PlatformKnowledgeSnapshot,
    ProjectStatus,
)
from app.schemas.scenario import ScenarioRequest
from app.security.crypto import MasterKeyBox
from app.services.scenario_service import ScenarioService


class ContentScenarioService:
    """Create and persist a scenario using only the brand's latest grounded snapshots."""

    _FACT_LIMITS = {
        "products_and_services": 3,
        "capabilities": 5,
        "workflows": 3,
        "target_audiences": 3,
        "differentiators": 3,
    }

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
        self.master_key_box = master_key_box

    async def generate(
        self,
        *,
        company_id: uuid.UUID,
        project_id: uuid.UUID,
        connection_id: uuid.UUID,
        timeout_seconds: float = 180.0,
    ) -> dict[str, Any]:
        project = await self.session.get(ContentProject, project_id)
        if project is None or project.company_id != company_id:
            raise NotFoundError("Content project not found for this company.")

        # A completed scenario is a safe replay result if a worker died after
        # persisting the project but before acknowledging its agent task.
        if project.status == ProjectStatus.SCRIPT_READY.value and project.scenario:
            return self._result(project, replayed=True)
        if project.status not in {ProjectStatus.DRAFT.value, ProjectStatus.SCRIPT_FAILED.value}:
            raise ConflictError(
                f"Scenario generation is not allowed from status '{project.status}'."
            )

        brand = await self.session.get(ContentBrand, project.brand_id)
        if brand is None or brand.company_id != company_id or brand.status != "active":
            raise NotFoundError("Active content brand not found for this company.")

        connection = await self.session.get(PlatformConnection, connection_id)
        if (
            connection is None
            or connection.company_id != company_id
            or connection.provider != "llm_openai"
            or connection.status not in {"connected", "degraded"}
        ):
            raise ConflictError("A validated llm_openai connection for this company is required.")

        platform_ids = list((await self.session.scalars(
            select(BrandPlatformLink.platform_id).where(
                BrandPlatformLink.company_id == company_id,
                BrandPlatformLink.brand_id == brand.id,
            )
        )).all())
        if not platform_ids:
            raise ConflictError(\n                "Link at least one monitored platform to this brand before generating a scenario."\n            )

        snapshots = list((await self.session.scalars(
            select(PlatformKnowledgeSnapshot)
            .where(
                PlatformKnowledgeSnapshot.company_id == company_id,
                PlatformKnowledgeSnapshot.platform_id.in_(platform_ids),
            )
            .order_by(PlatformKnowledgeSnapshot.extracted_at.desc())
        )).all())
        latest_by_platform: dict[str, PlatformKnowledgeSnapshot] = {}
        for snapshot in snapshots:
            latest_by_platform.setdefault(str(snapshot.platform_id), snapshot)
        if not latest_by_platform:
            raise ConflictError(\n                "No verified platform knowledge snapshot exists for the linked brand platforms."\n            )

        knowledge_context = self._build_knowledge_context(latest_by_platform.values())
        cta_config = brand.cta_config or {}
        visual_config = brand.visual_config or {}
        options = project.scene_options or {}
        product_context = "\n".join(
            value
            for value in (
                brand.description,
                visual_config.get("product_context"),
                visual_config.get("product_features"),
            )
            if isinstance(value, str) and value.strip()
        ) or None

        request = ScenarioRequest(
            topic=project.topic,
            target_duration_seconds=int(options.get("target_duration_seconds") or 30),
            scene_count=options.get("scene_count"),
            brand_name=brand.display_name or brand.name,
            brand_tone=brand.tone,
            character_name=visual_config.get("character_name") or brand.display_name or brand.name,
            character_description=(
                visual_config.get("character_description")
                or visual_config.get("character_reference_prompt")
            ),
            product_context=product_context,
            cta_text=cta_config.get("text"),
            hashtags=list(cta_config.get("hashtags") or []),
            language=brand.language,
            platform_knowledge_context=knowledge_context,
        )
        scenario, metadata = await ScenarioService(
            self.session, self.registry, self.executor, self.master_key_box
        ).generate(
            company_id=company_id,
            connection_id=connection_id,
            request=request,
            timeout_seconds=min(timeout_seconds, 180.0),
        )

        project.scenario = scenario.model_dump(mode="json")
        project.script_text = scenario.narration
        project.script_segments = [scene.model_dump(mode="json") for scene in scenario.scenes]
        project.video_title = scenario.title[:500]
        project.call_to_action = scenario.cta[:500] if scenario.cta else None
        usage = metadata.get("usage")
        if isinstance(usage, dict) and any(
            usage.get(key) is not None
            for key in ("prompt_tokens", "completion_tokens", "total_tokens", "cost", "total_cost")
        ):
            usage_metadata = dict(usage)
            if metadata.get("model"):
                usage_metadata["model"] = metadata["model"]
            usage_metadata.setdefault(
                "pricing_metadata",
                {"source": "llm_response_usage", "version": "openai_compatible_v1"},
            )
            project.llm_usage = usage_metadata
        project.status = ProjectStatus.SCRIPT_READY.value
        await self.session.flush()
        return self._result(project, replayed=False)

    @classmethod
    def _build_knowledge_context(cls, snapshots) -> str:
        compact: list[dict[str, Any]] = []
        for snapshot in snapshots:
            knowledge = snapshot.knowledge_json or {}
            item: dict[str, Any] = {
                "platform_id": str(snapshot.platform_id),
                "source_fingerprint": snapshot.source_fingerprint,
                "summary": str(knowledge.get("summary") or "")[:1000],
                "unknowns": [
                    str(value)[:180] for value in (knowledge.get("unknowns") or [])[:6]
                ],
            }
            for group, limit in cls._FACT_LIMITS.items():
                item[group] = [
                    {
                        "name": str(fact.get("name") or "")[:100],
                        "description": str(fact.get("description") or "")[:240],
                        "evidence": str(fact.get("evidence") or "")[:160],
                        "source_url": str(fact.get("source_url") or "")[:500],
                    }
                    for fact in (knowledge.get(group) or [])[:limit]
                    if isinstance(fact, dict)
                ]
            compact.append(item)
        while compact:
            encoded = json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
            if len(encoded) <= 11000:
                return encoded
            compact.pop()
        raise ConflictError(\n            "Linked platform knowledge could not be compacted into a usable context."\n        )

    @staticmethod
    def _result(project: ContentProject, *, replayed: bool) -> dict[str, Any]:
        scenario = project.scenario or {}
        return {
            "project_id": str(project.id),
            "project_status": project.status,
            "title": project.video_title or scenario.get("title"),
            "scene_count": len(project.script_segments or []),
            "total_duration_seconds": scenario.get("total_duration_seconds"),
            "replayed": replayed,
            "approval_required": True,
        }
