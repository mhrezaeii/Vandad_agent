"""Content production service for AI-native scenario and video workflows.

The active production path is scenario generation, operator approval, AI-provider
scene generation, provider-side composition, quality review and artifact delivery.
"""

from __future__ import annotations

import json
import uuid
from math import gcd
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    ConflictError,
    NotFoundError,
    UnprocessableEntityError,
)
from app.integrations.providers import (
    ProviderExecutor,
    ProviderRegistry,
)
from app.models import (
    BrandPlatformLink,
    ContentBrand,
    ContentBrandStatus,
    ContentProject,
    Platform,
    ProjectStatus,
    Workflow,
    WorkflowDefinitionStatus,
    WorkflowExecution,
    WorkflowTriggerType,
)
from app.security.authorization import CompanyScope
from app.security.crypto import MasterKeyBox
from app.services.audit_service import AuditService
from app.services.provider_budget_service import ProviderBudgetService
from app.services.workflow_execution_service import WorkflowExecutionService
from app.services.workflow_service import WorkflowService

BRAND_RESOURCE = "brand"
PROJECT_RESOURCE = "project"

SCRIPT_WORKFLOW_NAME = "vandefence.script.v1"
SCENE_SCRIPT_WORKFLOW_NAME = "vandad.scene.script.v1"
SCENE_VIDEO_WORKFLOW_NAME = "vandad.scene.video.v1"

VANDEFENCE_BRAND: dict[str, Any] = {
    "name": "Vandefence",
    "domain": "vandefence.ir",
    "industry": "Cybersecurity",
    "language": "fa-IR",
    "video_width": 1080,
    "video_height": 1920,
    "tone": (
        "حرفه‌ای، فنی و معتبر؛ لحن واضح و مطمئن بدون اغراق و بدون ادعاهای "
        "اثبات‌نشده درباره عملکرد یا امنیت."
    ),
    "visual_config": {
        "background": "dark navy",
        "background_color": "#0A1128",
        "accent": "electric blue",
        "accent_color": "#00A3FF",
        "style": "minimal cybersecurity graphics",
        # Rendering template (all optional assets are only used when present).
        "fit": "cover",
        "subtitle_font_size": 56,
        "subtitle_primary_color": "&H00FFFFFF",
        "subtitle_outline_color": "&H00101010",
        "logo_path": "assets/vandefence-logo.png",
        "logo_scale": 0.18,
        "logo_margin": 40,
        "music_path": None,
        "music_volume": 0.12,
        "intro_path": None,
        "outro_path": None,
    },
}

# Vandefence keeps its full existing profile; the extra profile fields are
# explicit so the idempotent seeder and the legacy bootstrap create identical
# rows.
VANDEFENCE_BRAND_SPEC: dict[str, Any] = {
    **VANDEFENCE_BRAND,
    "display_name": "Vandefence",
    "description": None,
    "cta_config": {},
}

# Canonical brand set seeded for every company (order is informational only).
# Only ``name``, ``display_name`` and safe defaults are provisioned here: no
# brand-specific business behavior is encoded, and no invented domain/tone is
# asserted for brands whose real profile is not known yet.
DEFAULT_BRANDS: tuple[dict[str, Any], ...] = (
    {
        "name": "NobatVakil",
        "display_name": "NobatVakil",
        "language": "fa-IR",
        "tone": None,
        "description": None,
        "cta_config": {},
    },
    {
        "name": "NobatMoshaver",
        "display_name": "NobatMoshaver",
        "language": "fa-IR",
        "tone": None,
        "description": None,
        "cta_config": {},
    },
    VANDEFENCE_BRAND_SPEC,
    {
        "name": "VakilBin",
        "display_name": "VakilBin",
        "language": "fa-IR",
        "tone": None,
        "description": None,
        "cta_config": {},
    },
    {
        "name": "Vandiar",
        "display_name": "Vandiar",
        "language": "fa-IR",
        "tone": None,
        "description": None,
        "cta_config": {},
    },
)

DEFAULT_BRAND_NAMES: tuple[str, ...] = tuple(spec["name"] for spec in DEFAULT_BRANDS)

SCRIPT_WORKFLOW_GRAPH: dict[str, Any] = {
    "nodes": [
        {"node_key": "start", "node_type": "trigger.manual", "name": "trigger"},
        {
            "node_key": "script_generation",
            "node_type": "action.provider",
            "name": "generate persian script",
            "configuration": {
                "connection_id": "__LLM_CONNECTION__",
                "operation": "execute",
                "response_format": "json_object",
                "temperature": 0.4,
                "max_tokens": 900,
            },
            "input_mapping": {
                "params": {
                    "$template": (
                        '{"system": "تو یک کپی‌رایتر فنی فارسی برای برند امنیتی Vandefence هستی. '
                        "خروجی را فقط به صورت JSON معتبر با کلیدهای title (عنوان کوتاه فارسی)، "
                        "script (متن گفتاری کامل فارسی)، segments (آرایه‌ای از جمله‌های کوتاه فارسی) "
                        "و cta (دعوت به اقدام کوتاه) برگردان. متن باید دقیق، محافظه‌کارانه و بدون "
                        "ادعای اثبات‌نشده درباره محصول، عملکرد، مشتری یا تضمین امنیتی باشد؛ از "
                        'بازاریابی ترس‌محور و اغراق پرهیز کن.", '
                        '"user": "یک اسکریپت گفتاری طبیعی و روان فارسی برای ویدیوی عمودی ۳۰ ثانیه‌ای '
                        "(حدود ۷۵ تا ۸۵ واژه) درباره موضوع زیر بنویس. ساختار: قلاب روشن در جمله اول، "
                        "توضیح فنی درست و ملموس در بدنه، جمع‌بندی کوتاه و CTA در پایان. اگر قابلیت "
                        "یا عدد مشخصی را نمی‌دانی، آن را ادعا نکن. موضوع: "
                        '{{ trigger.topic }}"}'
                    )
                }
            },
            "execution_settings": {"timeout_seconds": 120, "max_retries": 2},
        },
        {
            "node_key": "record_script",
            "node_type": "content.update_project",
            "name": "persist script",
            "input_mapping": {"project_id": "$trigger.project_id"},
        },
        {
            "node_key": "finish",
            "node_type": "control.complete",
            "name": "done",
            "configuration": {"outcome": "completed"},
        },
    ],
    "edges": [
        {"from_key": "start", "to_key": "script_generation"},
        {"from_key": "script_generation", "to_key": "record_script"},
        {"from_key": "record_script", "to_key": "finish"},
    ],
}

SCENE_SCRIPT_WORKFLOW_GRAPH: dict[str, Any] = {
    "nodes": [
        {"node_key": "start", "node_type": "trigger.manual", "name": "trigger"},
        {
            "node_key": "generate_scenario",
            "node_type": "content.generate_scenario",
            "name": "generate persian scenario",
            "configuration": {
                "connection_id": "__LLM_CONNECTION__",
                "target_duration_seconds": 30,
            },
            "input_mapping": {"project_id": "$trigger.project_id"},
            "execution_settings": {"timeout_seconds": 180, "max_retries": 1},
        },
        {
            "node_key": "finish",
            "node_type": "control.complete",
            "name": "done",
            "configuration": {"outcome": "completed"},
        },
    ],
    "edges": [
        {"from_key": "start", "to_key": "generate_scenario"},
        {"from_key": "generate_scenario", "to_key": "finish"},
    ],
}

SCENE_VIDEO_WORKFLOW_GRAPH: dict[str, Any] = {
    "nodes": [
        {"node_key": "start", "node_type": "trigger.manual", "name": "trigger"},
        {
            "node_key": "prepare",
            "node_type": "content.prepare_scenes",
            "name": "prepare scene jobs",
            "input_mapping": {"project_id": "$trigger.project_id"},
            "execution_settings": {"timeout_seconds": 60, "max_retries": 1},
        },
        {
            "node_key": "submit",
            "node_type": "content.submit_scene_videos",
            "name": "submit scene videos (idempotent)",
            "input_mapping": {"project_id": "$trigger.project_id"},
            "execution_settings": {"timeout_seconds": 120, "max_retries": 2},
        },
        {
            "node_key": "wait",
            "node_type": "control.delay",
            "name": "initial provider wait",
            "configuration": {"seconds": 20},
        },
        {
            "node_key": "poll",
            "node_type": "content.poll_scene_videos",
            "name": "poll scene statuses",
            "input_mapping": {"project_id": "$trigger.project_id"},
            # Still-generating is a transient error; the engine retries with
            # backoff from the persisted provider job ids. Never resubmits a
            # scene that already has a provider job id.
            "execution_settings": {
                "timeout_seconds": 120,
                "max_retries": 60,
                "backoff_seconds": 30,
                "idempotent": True,
            },
        },
        {
            "node_key": "retrieve",
            "node_type": "content.retrieve_scene_videos",
            "name": "download scene videos",
            "input_mapping": {"project_id": "$trigger.project_id"},
            "execution_settings": {"timeout_seconds": 900, "max_retries": 2},
        },
        {
            "node_key": "assemble",
            "node_type": "content.assemble_scenes",
            "name": "assemble final vertical video",
            "input_mapping": {"project_id": "$trigger.project_id"},
            "execution_settings": {"timeout_seconds": 1800, "max_retries": 1},
        },
        {
            "node_key": "finish",
            "node_type": "control.complete",
            "name": "done",
            "configuration": {"outcome": "completed"},
        },
    ],
    "edges": [
        {"from_key": "start", "to_key": "prepare"},
        {"from_key": "prepare", "to_key": "submit"},
        {"from_key": "submit", "to_key": "wait"},
        {"from_key": "wait", "to_key": "poll"},
        {"from_key": "poll", "to_key": "retrieve"},
        {"from_key": "retrieve", "to_key": "assemble"},
        {"from_key": "assemble", "to_key": "finish"},
    ],
}


def _bind_connection(graph: dict[str, Any], placeholder: str, connection_id: uuid.UUID) -> dict[str, Any]:
    """Return a deep copy of *graph* with the placeholder connection bound."""
    import copy

    bound = copy.deepcopy(graph)
    for node in bound["nodes"]:
        configuration = node.get("configuration") or {}
        if configuration.get("connection_id") == placeholder:
            configuration["connection_id"] = str(connection_id)
            node["configuration"] = configuration
    return bound


class ContentService:
    def __init__(
        self,
        session: AsyncSession,
        registry: ProviderRegistry,
        executor: ProviderExecutor,
        master_key_box: MasterKeyBox,
        workflow_queue=None,  # noqa: ANN001 - WorkflowQueue | None
        scene_video_registry=None,  # noqa: ANN001 - SceneVideoProviderRegistry | None
    ) -> None:
        self.session = session
        self.registry = registry
        self.executor = executor
        self._keys = master_key_box
        self.queue = workflow_queue
        self.scene_video_registry = scene_video_registry
        self.audit = AuditService(session)
        self._workflow_service = WorkflowService(session, registry)
        self._execution_service = WorkflowExecutionService(
            session, registry, executor, master_key_box, queue=workflow_queue
        )

    # ------------------------------------------------------------------ brands
    async def _find_brand_by_name(
        self, *, company_id: uuid.UUID, name: str
    ) -> ContentBrand | None:
        """Case-insensitive lookup of a company brand by its stable name."""
        result = await self.session.execute(
            select(ContentBrand)
            .where(
                ContentBrand.company_id == company_id,
                func.lower(ContentBrand.name) == name.lower(),
            )
            .order_by(ContentBrand.created_at)
        )
        return result.scalars().first()

    async def _ensure_brand(
        self, *, company_id: uuid.UUID, spec: dict[str, Any]
    ) -> ContentBrand:
        """Create one brand if absent; never mutates an existing profile."""
        brand = await self._find_brand_by_name(company_id=company_id, name=spec["name"])
        if brand is not None:
            return brand
        brand = ContentBrand(company_id=company_id, **spec)
        self.session.add(brand)
        await self.session.flush()
        self.audit.record(
            event_type="content.brand_created",
            action="create",
            company_id=company_id,
            resource_type=BRAND_RESOURCE,
            resource_id=brand.id,
            metadata={"name": brand.name, "domain": brand.domain},
        )
        await self.session.flush()
        return brand

    async def ensure_vandefence_brand(self, *, company_id: uuid.UUID) -> ContentBrand:
        """Idempotent first-brand bootstrap (kept for backward compatibility)."""
        return await self._ensure_brand(
            company_id=company_id, spec=VANDEFENCE_BRAND_SPEC
        )

    async def ensure_default_brands(self, *, company_id: uuid.UUID) -> list[ContentBrand]:
        """Idempotently provision the five default brands for a company.

        Safe to call on every request/startup: existing brands (including
        archived ones) are never recreated or overwritten.
        """
        brands = [
            await self._ensure_brand(company_id=company_id, spec=spec)
            for spec in DEFAULT_BRANDS
        ]
        await self.session.flush()
        return brands

    async def create_brand(
        self, *, company_id: uuid.UUID, data: dict[str, Any]
    ) -> ContentBrand:
        """Create a tenant-owned brand with company-scoped duplicate checks."""
        name = data["name"]
        if await self._find_brand_by_name(company_id=company_id, name=name) is not None:
            raise ConflictError(
                "A brand with this name already exists for the company.",
                details=[{"field": "name", "value": name}],
            )
        brand = ContentBrand(company_id=company_id, **data)
        try:
            # Savepoint so a uniqueness race does not discard the outer unit of
            # work; the constraint remains the final guard behind the check.
            async with self.session.begin_nested():
                self.session.add(brand)
                await self.session.flush()
        except IntegrityError as exc:
            raise ConflictError(
                "A brand with this name already exists for the company.",
                details=[{"field": "name", "value": name}],
            ) from exc
        self.audit.record(
            event_type="content.brand_created",
            action="create",
            company_id=company_id,
            resource_type=BRAND_RESOURCE,
            resource_id=brand.id,
            metadata={"name": brand.name, "domain": brand.domain},
        )
        await self.session.flush()
        return brand

    async def update_brand(
        self,
        brand_id: uuid.UUID,
        *,
        company_id: uuid.UUID,
        changes: dict[str, Any],
    ) -> ContentBrand:
        """Apply a partial update to a brand owned by *company_id*."""
        brand = await self.get_brand(brand_id, company_id=company_id)
        if not changes:
            return brand
        for field in ("language", "status"):
            if field in changes and changes[field] is None:
                raise UnprocessableEntityError(
                    f"'{field}' cannot be null.",
                    details=[{"field": field}],
                )
        if "cta_config" in changes and changes["cta_config"] is None:
            changes["cta_config"] = {}
        applied = sorted(changes)
        for field, value in changes.items():
            setattr(brand, field, value)
        await self.session.flush()
        self.audit.record(
            event_type="content.brand_updated",
            action="update",
            company_id=company_id,
            resource_type=BRAND_RESOURCE,
            resource_id=brand.id,
            metadata={"fields": applied},
        )
        await self.session.flush()
        return brand

    async def get_brand(self, brand_id: uuid.UUID, *, company_id: uuid.UUID) -> ContentBrand:
        brand = await self.session.get(ContentBrand, brand_id)
        if brand is None or brand.company_id != company_id:
            raise NotFoundError(
                "Brand not found.", details=[{"field": "brand_id", "value": str(brand_id)}]
            )
        return brand

    async def set_brand_platforms(
        self, brand_id: uuid.UUID, *, company_id: uuid.UUID, platform_ids: list[uuid.UUID]
    ) -> list[uuid.UUID]:
        """Replace a brand's linked platforms, enforcing same-company ownership."""
        brand = await self.get_brand(brand_id, company_id=company_id)
        unique_ids = list(dict.fromkeys(platform_ids))
        platforms = list((await self.session.scalars(
            select(Platform).where(
                Platform.company_id == company_id,
                Platform.id.in_(unique_ids),
            )
        )).all()) if unique_ids else []
        if len(platforms) != len(unique_ids):
            raise NotFoundError("One or more platforms were not found for this company.")
        existing = list((await self.session.scalars(
            select(BrandPlatformLink).where(
                BrandPlatformLink.company_id == company_id,
                BrandPlatformLink.brand_id == brand.id,
            )
        )).all())
        for link in existing:
            await self.session.delete(link)
        for platform_id in unique_ids:
            self.session.add(BrandPlatformLink(
                brand_id=brand.id, platform_id=platform_id, company_id=company_id
            ))
        await self.session.flush()
        self.audit.record(
            event_type="content.brand_platforms_updated",
            action="link_platforms",
            company_id=company_id,
            resource_type=BRAND_RESOURCE,
            resource_id=brand.id,
            metadata={"platform_ids": [str(value) for value in unique_ids]},
        )
        await self.session.flush()
        return unique_ids

    async def list_brand_platforms(
        self, brand_id: uuid.UUID, *, company_id: uuid.UUID
    ) -> list[Platform]:
        """List only platforms linked to a brand within the active tenant."""
        brand = await self.get_brand(brand_id, company_id=company_id)
        return list((await self.session.scalars(
            select(Platform)
            .join(BrandPlatformLink, BrandPlatformLink.platform_id == Platform.id)
            .where(
                BrandPlatformLink.company_id == company_id,
                BrandPlatformLink.brand_id == brand.id,
                Platform.company_id == company_id,
            )
            .order_by(Platform.name.asc())
        )).all())

    async def list_brands(
        self, *, company_id: uuid.UUID, include_archived: bool = False
    ) -> list[ContentBrand]:
        statement = select(ContentBrand).where(ContentBrand.company_id == company_id)
        if not include_archived:
            statement = statement.where(
                ContentBrand.status == ContentBrandStatus.ACTIVE.value
            )
        statement = statement.order_by(ContentBrand.name)
        return list((await self.session.execute(statement)).scalars().all())

    # ---------------------------------------------------------------- projects
    async def create_project(
        self, *, scope: CompanyScope, brand_id: uuid.UUID, topic: str
    ) -> ContentProject:
        brand = await self.get_brand(brand_id, company_id=scope.company_id)
        project = ContentProject(
            company_id=scope.company_id,
            brand_id=brand.id,
            topic=topic,
            status=ProjectStatus.DRAFT.value,
            video_mode="scene",
        )
        self.session.add(project)
        await self.session.flush()
        self.audit.record(
            event_type="content.project_created",
            action="create",
            company_id=scope.company_id,
            resource_type=PROJECT_RESOURCE,
            resource_id=project.id,
            metadata={"brand": brand.name, "topic": topic[:200]},
        )
        await self.session.flush()
        return project

    async def get_project(self, project_id: uuid.UUID, *, company_id: uuid.UUID) -> ContentProject:
        project = await self.session.get(ContentProject, project_id)
        if project is None or project.company_id != company_id:
            raise NotFoundError(
                "Project not found.", details=[{"field": "project_id", "value": str(project_id)}]
            )
        return project

    # --------------------------------------------------------- script + video
    async def generate_script(
        self, project_id: uuid.UUID, *, scope: CompanyScope, connection_id: uuid.UUID,
        task_id: uuid.UUID | None = None,
    ) -> ContentProject:
        project = await self.get_project(project_id, company_id=scope.company_id)
        if project.status not in (ProjectStatus.DRAFT.value, ProjectStatus.SCRIPT_FAILED.value):
            raise ConflictError(
                f"Script generation is not allowed from status '{project.status}'.",
                details=[{"status": project.status}],
            )
        await self.ensure_workflow(
            SCRIPT_WORKFLOW_NAME, SCRIPT_WORKFLOW_GRAPH, company_id=scope.company_id,
            connection_id=connection_id,
        )
        workflow = await self._require_published(SCRIPT_WORKFLOW_NAME, scope.company_id)
        execution, _created = await self._execution_service.execute_workflow(
            workflow.id,
            scope=scope,
            input_metadata={
                "project_id": str(project.id),
                "topic": project.topic,
                "agent_task_id": str(task_id) if task_id else None,
            },
            idempotency_key=f"script:{project.id}",
            trigger_type=WorkflowTriggerType.API,
            trigger_reference=str(project.id),
        )
        project.status = ProjectStatus.SCRIPT_GENERATING.value
        project.script_workflow_execution_id = execution.id
        self.audit.record(
            event_type="content.script_generation_started",
            action="generate_script",
            company_id=scope.company_id,
            resource_type=PROJECT_RESOURCE,
            resource_id=project.id,
            metadata={"execution_id": str(execution.id)},
        )
        await self.session.flush()
        # onupdate server defaults are not returned by UPDATE; reload so the
        # response serialization never triggers lazy IO outside the session.
        await self.session.refresh(project)
        return project

    async def approve_script(self, project_id: uuid.UUID, *, scope: CompanyScope) -> ContentProject:
        project = await self.get_project(project_id, company_id=scope.company_id)
        if project.status != ProjectStatus.SCRIPT_READY.value:
            raise ConflictError(
                f"Only script_ready projects can be approved (status '{project.status}').",
                details=[{"status": project.status}],
            )
        if not project.script_text:
            raise ConflictError("The project has no script to approve.")
        project.status = ProjectStatus.APPROVED.value
        self.audit.record(
            event_type="content.script_approved",
            action="approve_script",
            company_id=scope.company_id,
            resource_type=PROJECT_RESOURCE,
            resource_id=project.id,
            metadata={},
        )
        await self.session.flush()
        await self.session.refresh(project)
        return project

    # ------------------------------------------------------------ scene pipeline
    async def create_scene_project(
        self,
        *,
        scope: CompanyScope,
        brand_id: uuid.UUID,
        topic: str,
        target_duration_seconds: int = 30,
        scene_count: int | None = None,
        provider_id: str | None = None,
        narration_artifact_id: uuid.UUID | None = None,
    ) -> ContentProject:
        """Create a non-avatar project that will be built from scene clips."""
        from app.schemas.scenario import MAX_TARGET_DURATION_SECONDS, MIN_TARGET_DURATION_SECONDS

        if not MIN_TARGET_DURATION_SECONDS <= target_duration_seconds <= MAX_TARGET_DURATION_SECONDS:
            raise UnprocessableEntityError(
                "target_duration_seconds is outside the supported range.",
                details=[
                    {
                        "field": "target_duration_seconds",
                        "min": MIN_TARGET_DURATION_SECONDS,
                        "max": MAX_TARGET_DURATION_SECONDS,
                    }
                ],
            )
        brand = await self.get_brand(brand_id, company_id=scope.company_id)
        project = ContentProject(
            company_id=scope.company_id,
            brand_id=brand.id,
            topic=topic,
            status=ProjectStatus.DRAFT.value,
            video_mode="scene",
            scene_options={
                "target_duration_seconds": target_duration_seconds,
                "scene_count": scene_count,
                "provider_id": provider_id,
                "narration_artifact_id": (
                    str(narration_artifact_id) if narration_artifact_id else None
                ),
            },
            scene_video_provider=provider_id,
        )
        self.session.add(project)
        await self.session.flush()
        self.audit.record(
            event_type="content.scene_project_created",
            action="create",
            company_id=scope.company_id,
            resource_type=PROJECT_RESOURCE,
            resource_id=project.id,
            metadata={
                "brand": brand.name,
                "topic": topic[:200],
                "target_duration_seconds": target_duration_seconds,
                "provider_id": provider_id,
            },
        )
        await self.session.flush()
        return project

    async def generate_scene_scenario(
        self,
        project_id: uuid.UUID,
        *,
        scope: CompanyScope,
        connection_id: uuid.UUID,
        task_id: uuid.UUID | None = None,
    ) -> ContentProject:
        """Start scenario generation through the existing text API connection."""
        project = await self.get_project(project_id, company_id=scope.company_id)
        if project.video_mode != "scene":
            raise ConflictError("This project does not use the scene pipeline.")
        if project.status not in (
            ProjectStatus.DRAFT.value,
            ProjectStatus.SCRIPT_FAILED.value,
        ):
            raise ConflictError(
                f"Scenario generation is not allowed from status '{project.status}'.",
                details=[{"status": project.status}],
            )
        await self.ensure_workflow(
            SCENE_SCRIPT_WORKFLOW_NAME,
            SCENE_SCRIPT_WORKFLOW_GRAPH,
            company_id=scope.company_id,
            connection_id=connection_id,
        )
        workflow = await self._require_published(SCENE_SCRIPT_WORKFLOW_NAME, scope.company_id)
        options = project.scene_options or {}
        execution, _created = await self._execution_service.execute_workflow(
            workflow.id,
            scope=scope,
            input_metadata={
                "project_id": str(project.id),
                "topic": project.topic,
                "target_duration_seconds": options.get("target_duration_seconds", 30),
                "scene_count": options.get("scene_count"),
                "agent_task_id": str(task_id) if task_id else None,
            },
            idempotency_key=f"scene-script:{project.id}",
            trigger_type=WorkflowTriggerType.API,
            trigger_reference=str(project.id),
        )
        project.status = ProjectStatus.SCRIPT_GENERATING.value
        project.script_workflow_execution_id = execution.id
        self.audit.record(
            event_type="content.scene_scenario_generation_started",
            action="generate_scenario",
            company_id=scope.company_id,
            resource_type=PROJECT_RESOURCE,
            resource_id=project.id,
            metadata={"execution_id": str(execution.id)},
        )
        await self.session.flush()
        await self.session.refresh(project)
        return project

    async def approve_scene_scenario(
        self, project_id: uuid.UUID, *, scope: CompanyScope
    ) -> ContentProject:
        """Operator gate: only a generated scenario can be approved."""
        project = await self.get_project(project_id, company_id=scope.company_id)
        if project.video_mode != "scene":
            raise ConflictError("This project does not use the scene pipeline.")
        if project.status != ProjectStatus.SCRIPT_READY.value:
            raise ConflictError(
                f"Only script_ready projects can be approved (status '{project.status}').",
                details=[{"status": project.status}],
            )
        if not project.scenario:
            raise ConflictError("The project has no generated scenario to approve.")
        project.status = ProjectStatus.APPROVED.value
        self.audit.record(
            event_type="content.scene_scenario_approved",
            action="approve_scenario",
            company_id=scope.company_id,
            resource_type=PROJECT_RESOURCE,
            resource_id=project.id,
            metadata={"scene_count": len((project.scenario or {}).get("scenes") or [])},
        )
        await self.session.flush()
        await self.session.refresh(project)
        return project

    async def generate_scene_video(
        self,
        project_id: uuid.UUID,
        *,
        scope: CompanyScope,
        provider_id: str | None = None,
        task_id: uuid.UUID | None = None,
        provider_fallbacks: list[str] | None = None,
    ) -> ContentProject:
        """Start per-scene generation, polling, download and assembly."""
        project = await self.get_project(project_id, company_id=scope.company_id)
        if project.video_mode != "scene":
            raise ConflictError("This project does not use the scene pipeline.")
        if project.status != ProjectStatus.APPROVED.value:
            raise ConflictError(
                f"Only approved projects can generate video (status '{project.status}').",
                details=[{"status": project.status}],
            )
        if not project.scenario:
            raise ConflictError("The project has no generated scenario to render.")

        options = dict(project.scene_options or {})
        selected_provider = provider_id or options.get("provider_id") or project.scene_video_provider
        if not selected_provider:
            from app.core.config import get_settings

            selected_provider = get_settings().scene_video_provider
        if selected_provider == "mock":
            from app.core.config import get_settings

            if get_settings().is_production:
                raise ConflictError("The mock scene-video provider is forbidden in production.")
        if self.scene_video_registry is None:
            raise ConflictError("The scene-video provider registry is unavailable; generation was not reserved.")
        if selected_provider and self.scene_video_registry is not None:
            scenario_scenes = (project.scenario or {}).get("scenes", [])
            fallback_ids = (
                provider_fallbacks
                if provider_fallbacks is not None
                else options.get("provider_fallbacks", [])
            )
            ordered_ids = [selected_provider]
            if isinstance(fallback_ids, list):
                ordered_ids.extend(
                    str(item) for item in fallback_ids
                    if isinstance(item, str) and item and item not in ordered_ids
                )
            from app.integrations.video import SceneVideoProviderRegistryError

            brand = await self.session.get(ContentBrand, project.brand_id)
            if brand is None or brand.company_id != scope.company_id:
                raise NotFoundError("Brand not found for this project.")
            required_durations = tuple(
                float(scene["duration_seconds"])
                for scene in scenario_scenes
                if isinstance(scene, dict)
                and isinstance(scene.get("duration_seconds"), (int, float))
                and not isinstance(scene.get("duration_seconds"), bool)
            )
            divisor = gcd(int(brand.video_width), int(brand.video_height))
            required_aspect_ratio = (
                f"{int(brand.video_width) // divisor}:{int(brand.video_height) // divisor}"
            )
            try:
                selected = await self.scene_video_registry.resolve_ordered_healthy(
                    ordered_ids,
                    require_composition=True,
                    require_reference_image=bool(options.get("requires_reference_image", False)),
                    require_quality_review=True,
                    required_durations=required_durations,
                    required_aspect_ratio=required_aspect_ratio,
                    required_language=brand.language,
                )
            except SceneVideoProviderRegistryError as exc:
                raise UnprocessableEntityError(
                    "No configured scene-video provider satisfies the required production capabilities.",
                    details=[{"field": "provider_id", "value": selected_provider}],
                ) from exc
            selected_provider = selected.provider_id
            project.scene_video_provider = selected_provider
            options["provider_id"] = selected_provider
            project.scene_options = options
        if selected_provider:
            options["provider_id"] = selected_provider
            project.scene_options = options
            project.scene_video_provider = selected_provider

        reservation = None
        if selected_provider != "mock":
            reservation = await ProviderBudgetService(self.session).reserve(
                scope.company_id,
                selected_provider,
                "content.scene.generate_video",
                idempotency_key=f"scene-video:{project.id}",
                require_budget=True,
                task_id=task_id,
            )
            project.cost_metadata = {
                "provider_id": selected_provider,
                "budget_reservation_id": str(reservation.id) if reservation else None,
                "status": "reserved",
            }
            await self.session.commit()
        else:
            project.cost_metadata = {"provider_id": "mock", "status": "not_billable"}

        try:
            await self.ensure_workflow(
                SCENE_VIDEO_WORKFLOW_NAME,
                SCENE_VIDEO_WORKFLOW_GRAPH,
                company_id=scope.company_id,
                connection_id=None,
            )
            workflow = await self._require_published(SCENE_VIDEO_WORKFLOW_NAME, scope.company_id)
            execution, _created = await self._execution_service.execute_workflow(
                workflow.id,
                scope=scope,
                input_metadata={
                    "project_id": str(project.id),
                    "provider_id": selected_provider,
                    "agent_task_id": str(task_id) if task_id else None,
                    "narration_artifact_id": options.get("narration_artifact_id"),
                },
                idempotency_key=f"scene-video:{project.id}",
                trigger_type=WorkflowTriggerType.API,
                trigger_reference=str(project.id),
            )
        except Exception:
            if reservation is not None:
                await ProviderBudgetService(self.session).settle(
                    reservation, error_code="scene_video_dispatch_failed"
                )
            raise
        project.status = ProjectStatus.VIDEO_GENERATION_PENDING.value
        project.video_workflow_execution_id = execution.id
        project.error_message = None
        self.audit.record(
            event_type="content.scene_video_generation_started",
            action="generate_video",
            company_id=scope.company_id,
            resource_type=PROJECT_RESOURCE,
            resource_id=project.id,
            metadata={
                "execution_id": str(execution.id),
                "provider": selected_provider,
            },
        )
        await self.session.flush()
        await self.session.refresh(project)
        return project

    async def cancel_scene_video(
        self, project_id: uuid.UUID, *, scope: CompanyScope
    ) -> ContentProject:
        """Cancel in-flight scene jobs and the workflow execution."""
        project = await self.get_project(project_id, company_id=scope.company_id)
        if project.video_mode != "scene":
            raise ConflictError("This project does not use the scene pipeline.")
        if project.status in (
            ProjectStatus.COMPLETED.value,
            ProjectStatus.CANCELLED.value,
        ):
            raise ConflictError(
                f"The project cannot be cancelled from status '{project.status}'.",
                details=[{"status": project.status}],
            )
        if project.video_workflow_execution_id and self.scene_video_registry is not None:
            from app.services.scene_video_service import SceneVideoService

            service = SceneVideoService(
                self.session,
                self.scene_video_registry,
                storage=None,
            )
            await service.cancel(project)
        elif project.video_workflow_execution_id:
            project.status = ProjectStatus.CANCELLED.value
        if project.video_workflow_execution_id:
            try:
                await self._execution_service.cancel_execution(
                    project.video_workflow_execution_id, scope=scope
                )
            except ConflictError:
                # The execution may already be terminal; cancellation of the
                # project record still stands.
                pass
        project.status = ProjectStatus.CANCELLED.value
        project.error_message = None
        self.audit.record(
            event_type="content.scene_video_cancelled",
            action="cancel",
            company_id=scope.company_id,
            resource_type=PROJECT_RESOURCE,
            resource_id=project.id,
            metadata={},
        )
        await self.session.flush()
        await self.session.refresh(project)
        return project

    async def list_scene_jobs(
        self, project_id: uuid.UUID, *, scope: CompanyScope
    ) -> list[Any]:
        from app.models import SceneVideoJob

        project = await self.get_project(project_id, company_id=scope.company_id)
        return list(
            (
                await self.session.execute(
                    select(SceneVideoJob)
                    .where(SceneVideoJob.project_id == project.id)
                    .order_by(SceneVideoJob.scene_index)
                )
            ).scalars()
        )

    # ------------------------------------------------------------- introspection
    async def get_job_status(self, job_id: uuid.UUID, *, company_id: uuid.UUID) -> dict[str, Any]:
        project = (
            await self.session.execute(
                select(ContentProject).where(
                    ContentProject.video_workflow_execution_id == job_id
                )
            )
        ).scalar_one_or_none()
        if project is None or project.company_id != company_id:
            raise NotFoundError("Job not found.", details=[{"job_id": str(job_id)}])
        execution = None
        if project.video_workflow_execution_id:
            execution = await self.session.get(WorkflowExecution, project.video_workflow_execution_id)
        scene_jobs: list[dict[str, Any]] = []
        if project.video_mode == "scene":
            from app.models import SceneVideoJob

            jobs = (
                await self.session.execute(
                    select(SceneVideoJob)
                    .where(SceneVideoJob.project_id == project.id)
                    .order_by(SceneVideoJob.scene_index)
                )
            ).scalars()
            scene_jobs = [
                {
                    "scene_index": job.scene_index,
                    "status": job.status,
                    "provider": job.provider,
                    "provider_job_id": job.provider_job_id,
                    "progress": job.progress,
                    "attempts": job.attempts,
                    "artifact_id": str(job.artifact_id) if job.artifact_id else None,
                    "error_code": job.error_code,
                }
                for job in jobs
            ]
        return {
            "project_id": str(project.id),
            "status": project.status,
            "video_mode": project.video_mode,
            "provider_job_id": project.provider_job_id,
            "provider_status": project.provider_status,
            "scene_video_provider": project.scene_video_provider,
            "scene_jobs": scene_jobs,
            "assembly_metadata": project.assembly_metadata,
            "raw_artifact_id": str(project.raw_artifact_id) if project.raw_artifact_id else None,
            "final_artifact_id": str(project.final_artifact_id) if project.final_artifact_id else None,
            "execution_status": execution.status if execution else None,
            "cost_metadata": project.cost_metadata,
            "llm_usage": project.llm_usage,
        }

    async def list_projects(
        self, *, company_id: uuid.UUID, limit: int = 50, offset: int = 0
    ) -> list[ContentProject]:
        rows = await self.session.scalars(
            select(ContentProject)
            .where(ContentProject.company_id == company_id)
            .order_by(ContentProject.updated_at.desc(), ContentProject.id.desc())
            .limit(max(1, min(limit, 100)))
            .offset(max(0, offset))
        )
        return list(rows.all())

    async def list_videos(self, *, company_id: uuid.UUID) -> list[ContentProject]:
        return list(
            (
                await self.session.execute(
                    select(ContentProject)
                    .where(
                        ContentProject.company_id == company_id,
                        ContentProject.status == ProjectStatus.COMPLETED.value,
                    )
                    .order_by(ContentProject.updated_at.desc())
                )
            ).scalars().all()
        )

    # ---------------------------------------------------------- workflow seeds
    async def ensure_workflow(
        self,
        name: str,
        graph: dict[str, Any],
        *,
        company_id: uuid.UUID,
        connection_id: uuid.UUID | None,
    ) -> Workflow:
        """Idempotently ensure a published workflow bound to the given connection.

        Re-binds (new draft + publish) only when the stored graph references a
        different connection than the operator selected. ``connection_id`` is
        optional for graphs that resolve their provider from project settings.
        """
        bound_graph = graph
        if connection_id is not None:
            llm_placeholder = "__LLM_CONNECTION__" if "script" in name else "__HEYGEN_CONNECTION__"
            bound_graph = _bind_connection(graph, llm_placeholder, connection_id)
            bound_graph = _bind_connection(bound_graph, "__HEYGEN_CONNECTION__", connection_id)

        workflow = (
            await self.session.execute(
                select(Workflow).where(
                    Workflow.company_id == company_id, Workflow.name == name
                )
            )
        ).scalar_one_or_none()
        if workflow is not None:
            if workflow.status == WorkflowDefinitionStatus.PUBLISHED.value and workflow.current_version_id:
                from app.workflows.validation import load_version_graph

                nodes, _edges = await load_version_graph(self.session, workflow.current_version_id)
                used_connections = {
                    node.configuration.get("connection_id")
                    for node in nodes
                    if node.configuration.get("connection_id")
                }
                if connection_id is None or str(connection_id) in used_connections:
                    return workflow
            scope = CompanyScope(company_id=company_id, principal=_system_principal())
            await self._workflow_service.update_workflow(
                workflow.id,
                company_id=company_id,
                nodes=bound_graph["nodes"],
                edges=bound_graph["edges"],
            )
            await self._workflow_service.publish_workflow(workflow.id, company_id=company_id)
            await self.session.refresh(workflow)
            return workflow

        scope = CompanyScope(company_id=company_id, principal=_system_principal())
        workflow = await self._workflow_service.create_workflow(
            scope=scope,
            name=name,
            description=f"Auto-provisioned {name} pipeline",
            nodes=bound_graph["nodes"],
            edges=bound_graph["edges"],
        )
        return await self._workflow_service.publish_workflow(workflow.id, company_id=company_id)

    async def _require_published(self, name: str, company_id: uuid.UUID) -> Workflow:
        workflow = (
            await self.session.execute(
                select(Workflow).where(
                    Workflow.company_id == company_id, Workflow.name == name
                )
            )
        ).scalar_one_or_none()
        if workflow is None or workflow.status != WorkflowDefinitionStatus.PUBLISHED.value:
            raise ConflictError(f"Required workflow '{name}' is not published for this company.")
        return workflow




def _system_principal():
    from app.security.authorization import Principal  # noqa: PLC0415

    return Principal(subject="content-service", company_ids=frozenset({"*"}))
