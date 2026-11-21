"""Backend-owned one-click content production and deterministic analysis."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.agents.runtime import AgentExecutionContext, ContentAgentHandler
from app.core.config import get_settings
from app.core.exceptions import ConflictError
from app.integrations.video import build_scene_video_registry
from app.integrations.video.mock import MockSceneVideoProvider
from app.models import (
    BrandPlatformLink,
    ContentBrand,
    ContentProject,
    PlatformKnowledgeSnapshot,
    ProjectStatus,
    WorkflowExecution,
)
from app.services.content_analysis_service import ContentAnalysisService
from app.services.content_scenario_service import ContentScenarioService
from app.services.content_service import ContentService
from app.services.platform_monitor_service import PlatformMonitorService


def _context(
    company_id,
    task_id,
    session_factory,
    payload,
    stages=None,
    *,
    scene_video_registry=None,
    default_scene_video_provider="mock",
):
    async def progress(stage, details):
        if stages is not None:
            stages.append((stage, details))

    return AgentExecutionContext(
        company_id=company_id,
        task_id=task_id,
        run_id=uuid.uuid4(),
        worker_id="content-production-test",
        payload=payload,
        session_factory=session_factory,
        provider_registry=object(),
        provider_executor=object(),
        master_key_box=object(),
        workflow_queue=object(),
        scene_video_registry=(
            object() if scene_video_registry is None else scene_video_registry
        ),
        default_scene_video_provider=default_scene_video_provider,
        progress_callback=progress,
    )


def _scenario_payload(*, title="معرفی ونداد"):
    scenes = [
        {
            "index": 0,
            "narration": "محصولتان را بهتر معرفی کنید.",
            "visual_prompt": "A clean cinematic product interface shot",
            "duration_seconds": 15.0,
            "scene_type": "product_demo",
        },
        {
            "index": 1,
            "narration": "پایش منابع را ساده و شفاف انجام دهید.",
            "visual_prompt": "A calm dashboard shot with charts",
            "duration_seconds": 15.0,
            "scene_type": "product_demo",
        },
    ]
    return {
        "title": title,
        "opening_hook": "محصولتان چقدر شناخته شده است؟",
        "scenes": scenes,
        "narration": " ".join(scene["narration"] for scene in scenes),
        "cta": "بیشتر آشنا شوید",
        "caption": "معرفی ونداد",
        "hashtags": ["#ونداد"],
        "target_duration_seconds": 30,
    }


async def _seed_brand(
    db_session, company, platform, *, link=True, snapshot=True, snapshot_age_hours=48
):
    brand = ContentBrand(
        company_id=company.id,
        name=f"brand-{uuid.uuid4().hex[:8]}",
        display_name="ونداد",
        description="سامانه هوشمند پایش و تولید محتوای تبلیغاتی",
        language="fa-IR",
        tone="حرفه‌ای و صمیمی",
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
    if snapshot:
        db_session.add(
            PlatformKnowledgeSnapshot(
                company_id=company.id,
                platform_id=platform.id,
                source_document_ids=[str(uuid.uuid4())],
                source_fingerprint="b" * 64,
                extracted_at=datetime.now(UTC) - timedelta(hours=snapshot_age_hours),
                knowledge_json={
                    "summary": "ونداد پایش منابع و تولید سناریو را مدیریت می‌کند.",
                    "capabilities": [
                        {
                            "name": "پایش پلتفرم",
                            "description": "شناسایی تغییرات و استخراج اطلاعات محصول",
                            "evidence": "پایش پلتفرم و استخراج اطلاعات",
                            "source_url": "https://product.example.test/features",
                            "confidence": 0.9,
                        },
                        {
                            "name": "پایش پلتفرم",
                            "description": "تکرار عمدی برای حذف نامزد تکراری",
                            "evidence": "پایش پلتفرم",
                            "source_url": "https://product.example.test/features",
                            "confidence": 0.8,
                        },
                    ],
                    "products_and_services": [
                        {
                            "name": "تولید سناریوی تبلیغاتی",
                            "description": "ساخت سناریوی فارسی از دانش تأییدشده",
                            "evidence": "تولید سناریو",
                            "source_url": "https://product.example.test/scenario",
                            "confidence": 0.85,
                        }
                    ],
                    "workflows": [],
                    "target_audiences": [],
                    "differentiators": [],
                    "unknowns": [],
                },
            )
        )
    await db_session.commit()
    return brand


async def test_analysis_deduplicates_and_preserves_sources(
    db_session, company_and_platform
):
    company, platform = company_and_platform
    brand = await _seed_brand(db_session, company, platform)

    analysis = await ContentAnalysisService(db_session).select(
        company_id=company.id, brand_id=brand.id, limit=10
    )

    assert analysis["selected_topic"]
    assert analysis["duplicate_count"] == 1
    assert analysis["platform_count"] == 1
    assert analysis["source_count"] == 2
    assert len(analysis["snapshot_ids"]) == 1
    topics = [item["topic"] for item in analysis["candidates"]]
    assert len(topics) == len(set(topics))
    assert "https://product.example.test/features" in {
        item["source_url"] for item in analysis["sources"]
    }
    assert 0 < analysis["brand_fit"] <= 1


async def test_production_runs_monitoring_scenario_video_and_is_retry_safe(
    db_session, session_factory, company_and_platform, monkeypatch
):
    company, platform = company_and_platform
    brand = await _seed_brand(db_session, company, platform)
    task_id = uuid.uuid4()
    captured: dict[str, object] = {"monitor": 0, "scenario": 0, "video": 0}

    async def fake_monitor(self, *, brand_id, company_id, connection_id=None):
        captured["monitor"] = int(captured["monitor"]) + 1
        assert brand_id == brand.id
        assert company_id == company.id
        return {
            "status": "completed",
            "platforms": [{"platform": "acme-admin", "status": "ok"}],
            "snapshot_available": True,
            "successful_platforms": 1,
        }

    async def fake_generate(
        self,
        *,
        company_id,
        project_id,
        connection_id=None,
        timeout_seconds=180.0,
        task_id=None,
    ):
        captured["scenario"] = int(captured["scenario"]) + 1
        project = await self.session.get(ContentProject, project_id)
        assert project is not None
        scenario = _scenario_payload()
        project.scenario = scenario
        project.script_text = scenario["narration"]
        project.script_segments = scenario["scenes"]
        project.video_title = scenario["title"]
        project.call_to_action = scenario["cta"]
        project.status = ProjectStatus.SCRIPT_READY.value
        await self.session.commit()
        return {
            "project_id": str(project_id),
            "project_status": project.status,
            "title": scenario["title"],
            "scene_count": 1,
            "replayed": False,
        }

    async def fake_generate_video(
        self,
        project_id,
        *,
        scope,
        provider_id=None,
        task_id=None,
        provider_fallbacks=None,
    ):
        captured["video"] = int(captured["video"]) + 1
        project = await self.session.get(ContentProject, project_id)
        assert project is not None
        assert project.status == ProjectStatus.APPROVED.value
        execution = WorkflowExecution(
            company_id=scope.company_id,
            workflow_name="scene video test",
            workflow_type="video_production",
            status="running",
        )
        self.session.add(execution)
        await self.session.flush()
        project.status = ProjectStatus.COMPLETED.value
        project.final_artifact_id = uuid.uuid4()
        project.video_workflow_execution_id = execution.id
        project.assembly_metadata = {
            "quality_review": {"status": "passed", "accepted": True}
        }
        await self.session.commit()
        return project

    monkeypatch.setattr(PlatformMonitorService, "monitor_brand", fake_monitor)
    monkeypatch.setattr(ContentScenarioService, "generate", fake_generate)
    monkeypatch.setattr(ContentService, "generate_scene_video", fake_generate_video)

    stages: list[tuple[str, dict]] = []
    context = _context(
        company.id,
        task_id,
        session_factory,
        {
            "brand_id": str(brand.id),
                        "target_duration_seconds": 30,
            "video_wait_seconds": 0,
        },
        stages,
    )
    handler = ContentAgentHandler()
    result = await handler.execute("content.production.run", context)

    assert result["project_status"] == "completed"
    assert result["video_status"] == "completed"
    assert result["final_video_ready"] is True
    assert result["qc_status"] == "passed"
    assert result["final_artifact_id"]
    assert captured["monitor"] == 1
    assert captured["scenario"] == 1
    assert captured["video"] == 1

    stage_names = [stage for stage, _details in stages]
    assert "monitoring.completed" in stage_names
    assert "scenario.completed" in stage_names
    assert "scenario.approved" in stage_names
    assert "video.dispatched" in stage_names
    assert "delivery.completed" in stage_names

    project = await db_session.scalar(
        select(ContentProject).where(ContentProject.id == uuid.UUID(result["project_id"]))
    )
    assert project is not None
    stored = project
    assert stored.scene_options["agent_task_id"] == str(task_id)

    replay = await handler.execute("content.production.run", context)
    assert replay["project_status"] == "completed"
    assert captured["monitor"] == 1
    assert captured["scenario"] == 1
    assert captured["video"] == 1


async def test_production_blocks_without_source_or_linked_platform(
    db_session, session_factory, company_and_platform
):
    company, _platform = company_and_platform
    brand = await _seed_brand(db_session, company, _platform, link=False, snapshot=False)
    context = _context(
        company.id,
        uuid.uuid4(),
        session_factory,
        {"brand_id": str(brand.id)},
    )

    with pytest.raises(ConflictError, match="No monitored source"):
        await ContentAgentHandler().execute("content.production.run", context)


async def test_production_fails_cleanly_when_monitoring_has_no_knowledge(
    db_session, session_factory, company_and_platform, monkeypatch
):
    company, platform = company_and_platform
    brand = await _seed_brand(db_session, company, platform, snapshot=False)

    async def fake_monitor(self, *, brand_id, company_id, connection_id=None):
        return {
            "status": "no_knowledge",
            "platforms": [{"platform": "acme-admin", "status": "error", "detail": "Timeout"}],
            "snapshot_available": False,
        }

    monkeypatch.setattr(PlatformMonitorService, "monitor_brand", fake_monitor)
    context = _context(
        company.id,
        uuid.uuid4(),
        session_factory,
        {"brand_id": str(brand.id)},
    )

    with pytest.raises(ConflictError, match="did not produce verified knowledge"):
        await ContentAgentHandler().execute("content.production.run", context)

    count = await db_session.scalar(
        select(ContentProject).where(ContentProject.brand_id == brand.id)
    )
    assert count is None


async def test_production_review_failure_marks_project_script_failed(
    db_session, session_factory, company_and_platform, monkeypatch
):
    company, platform = company_and_platform
    brand = await _seed_brand(db_session, company, platform)

    async def fake_monitor(self, *, brand_id, company_id, connection_id=None):
        return {
            "status": "completed",
            "platforms": [{"platform": "acme-admin", "status": "ok"}],
            "snapshot_available": True,
        }

    async def fake_generate(self, *, company_id, project_id, **kwargs):
        project = await self.session.get(ContentProject, project_id)
        scenario = _scenario_payload(title="تضمین نتیجه با ونداد")
        project.scenario = scenario
        project.script_segments = scenario["scenes"]
        project.status = ProjectStatus.SCRIPT_READY.value
        await self.session.commit()
        return {"project_id": str(project_id), "project_status": project.status}

    monkeypatch.setattr(PlatformMonitorService, "monitor_brand", fake_monitor)
    monkeypatch.setattr(ContentScenarioService, "generate", fake_generate)
    context = _context(
        company.id,
        uuid.uuid4(),
        session_factory,
        {"brand_id": str(brand.id), "video_wait_seconds": 0},
    )

    with pytest.raises(ConflictError, match="quality review failed"):
        await ContentAgentHandler().execute("content.production.run", context)

    project = await db_session.scalar(
        select(ContentProject).where(ContentProject.brand_id == brand.id)
    )
    assert project is not None
    assert project.status == ProjectStatus.SCRIPT_FAILED.value


async def test_production_regenerates_scenario_once_after_a_rejected_output(
    db_session, session_factory, company_and_platform, monkeypatch
):
    company, platform = company_and_platform
    brand = await _seed_brand(db_session, company, platform)
    calls = {"count": 0}

    async def fake_monitor(self, *, brand_id, company_id, connection_id=None):
        return {
            "status": "completed",
            "platforms": [{"platform": "acme-admin", "status": "ok"}],
            "snapshot_available": True,
        }

    async def fake_generate(self, *, company_id, project_id, **kwargs):
        calls["count"] += 1
        project = await self.session.get(ContentProject, project_id)
        scenario = _scenario_payload()
        if calls["count"] == 1:
            # Deterministically rejected by the quality gate: the scene total
            # does not match the requested duration.
            for scene in scenario["scenes"]:
                scene["duration_seconds"] = 5.0
        project.scenario = scenario
        project.script_segments = scenario["scenes"]
        project.status = ProjectStatus.SCRIPT_READY.value
        await self.session.commit()
        return {"project_id": str(project_id), "project_status": project.status}

    async def fake_generate_video(
        self,
        project_id,
        *,
        scope,
        provider_id=None,
        task_id=None,
        provider_fallbacks=None,
    ):
        project = await self.session.get(ContentProject, project_id)
        execution = WorkflowExecution(
            company_id=scope.company_id,
            workflow_name="scene video test",
            workflow_type="video_production",
            status="running",
        )
        self.session.add(execution)
        await self.session.flush()
        project.status = ProjectStatus.VIDEO_GENERATING.value
        project.video_workflow_execution_id = execution.id
        await self.session.commit()
        return project

    monkeypatch.setattr(PlatformMonitorService, "monitor_brand", fake_monitor)
    monkeypatch.setattr(ContentScenarioService, "generate", fake_generate)
    monkeypatch.setattr(ContentService, "generate_scene_video", fake_generate_video)
    context = _context(
        company.id,
        uuid.uuid4(),
        session_factory,
        {"brand_id": str(brand.id), "video_wait_seconds": 0},
    )

    result = await ContentAgentHandler().execute("content.production.run", context)

    assert calls["count"] == 2
    assert result["project_status"] == ProjectStatus.VIDEO_GENERATING.value
    assert result["video_status"] == "in_progress"
    assert result["title"] == "معرفی ونداد"


async def test_production_returns_in_progress_when_video_wait_is_bounded(
    db_session, session_factory, company_and_platform, monkeypatch
):
    company, platform = company_and_platform
    brand = await _seed_brand(db_session, company, platform)

    async def fake_monitor(self, *, brand_id, company_id, connection_id=None):
        return {
            "status": "completed",
            "platforms": [{"platform": "acme-admin", "status": "ok"}],
            "snapshot_available": True,
        }

    async def fake_generate(self, *, company_id, project_id, **kwargs):
        project = await self.session.get(ContentProject, project_id)
        scenario = _scenario_payload()
        project.scenario = scenario
        project.script_segments = scenario["scenes"]
        project.status = ProjectStatus.SCRIPT_READY.value
        await self.session.commit()
        return {"project_id": str(project_id), "project_status": project.status}

    async def fake_generate_video(
        self,
        project_id,
        *,
        scope,
        provider_id=None,
        task_id=None,
        provider_fallbacks=None,
    ):
        project = await self.session.get(ContentProject, project_id)
        execution = WorkflowExecution(
            company_id=scope.company_id,
            workflow_name="scene video test",
            workflow_type="video_production",
            status="running",
        )
        self.session.add(execution)
        await self.session.flush()
        project.status = ProjectStatus.VIDEO_GENERATING.value
        project.video_workflow_execution_id = execution.id
        await self.session.commit()
        return project

    monkeypatch.setattr(PlatformMonitorService, "monitor_brand", fake_monitor)
    monkeypatch.setattr(ContentScenarioService, "generate", fake_generate)
    monkeypatch.setattr(ContentService, "generate_scene_video", fake_generate_video)
    context = _context(
        company.id,
        uuid.uuid4(),
        session_factory,
        {"brand_id": str(brand.id), "video_wait_seconds": 0},
    )

    result = await ContentAgentHandler().execute("content.production.run", context)

    assert result["project_status"] == ProjectStatus.VIDEO_GENERATING.value
    assert result["video_status"] == "in_progress"
    assert result["final_video_ready"] is False
    assert result["final_artifact_id"] is None
    assert result["workflow_execution_id"]


async def test_single_shot_guard_blocks_reference_requiring_provider(
    db_session, session_factory, company_and_platform, monkeypatch
):
    company, platform = company_and_platform
    brand = await _seed_brand(db_session, company, platform)
    monitored = {"called": False}

    async def fake_monitor(self, **kwargs):
        monitored["called"] = True
        return {"status": "completed", "platforms": [], "snapshot_available": True}

    class ReferenceProvider(MockSceneVideoProvider):
        requires_reference_image = True

    registry = build_scene_video_registry(include_mock=False)
    registry.register(ReferenceProvider())

    monkeypatch.setattr(
        "app.core.config.get_settings",
        lambda: get_settings().model_copy(
            update={"scene_video_generation_mode": "single_shot"}
        ),
    )
    monkeypatch.setattr(PlatformMonitorService, "monitor_brand", fake_monitor)
    context = _context(
        company.id,
        uuid.uuid4(),
        session_factory,
        {"brand_id": str(brand.id)},
        scene_video_registry=registry,
    )

    with pytest.raises(ConflictError, match="character reference image"):
        await ContentAgentHandler().execute("content.production.run", context)
    assert monitored["called"] is False


async def test_single_shot_guard_allows_provider_without_reference_requirement(
    db_session, session_factory, company_and_platform, monkeypatch
):
    company, platform = company_and_platform
    brand = await _seed_brand(db_session, company, platform)

    class PipelineReached(RuntimeError):
        pass

    async def fake_monitor(self, **kwargs):
        raise PipelineReached

    registry = build_scene_video_registry(include_mock=True)
    monkeypatch.setattr(
        "app.core.config.get_settings",
        lambda: get_settings().model_copy(
            update={"scene_video_generation_mode": "single_shot"}
        ),
    )
    monkeypatch.setattr(PlatformMonitorService, "monitor_brand", fake_monitor)
    context = _context(
        company.id,
        uuid.uuid4(),
        session_factory,
        {"brand_id": str(brand.id)},
        scene_video_registry=registry,
    )

    with pytest.raises(PipelineReached):
        await ContentAgentHandler().execute("content.production.run", context)


async def test_production_reuses_fresh_knowledge_without_monitoring(
    db_session, session_factory, company_and_platform, monkeypatch
):
    company, platform = company_and_platform
    brand = await _seed_brand(
        db_session, company, platform, snapshot_age_hours=1
    )
    monitored = {"called": False}

    async def fake_monitor(self, **kwargs):
        monitored["called"] = True
        return {"status": "completed", "platforms": [], "snapshot_available": True}

    async def fake_generate(self, *, company_id, project_id, **kwargs):
        project = await self.session.get(ContentProject, project_id)
        scenario = _scenario_payload()
        project.scenario = scenario
        project.script_segments = scenario["scenes"]
        project.status = ProjectStatus.SCRIPT_READY.value
        await self.session.commit()
        return {"project_id": str(project_id), "project_status": project.status}

    async def fake_generate_video(self, project_id, *, scope, **kwargs):
        project = await self.session.get(ContentProject, project_id)
        execution = WorkflowExecution(
            company_id=scope.company_id,
            workflow_name="scene video test",
            workflow_type="video_production",
            status="running",
        )
        self.session.add(execution)
        await self.session.flush()
        project.status = ProjectStatus.COMPLETED.value
        project.final_artifact_id = uuid.uuid4()
        project.video_workflow_execution_id = execution.id
        project.assembly_metadata = {
            "quality_review": {"status": "passed", "accepted": True}
        }
        await self.session.commit()
        return project

    monkeypatch.setattr(PlatformMonitorService, "monitor_brand", fake_monitor)
    monkeypatch.setattr(ContentScenarioService, "generate", fake_generate)
    monkeypatch.setattr(ContentService, "generate_scene_video", fake_generate_video)

    stages: list[tuple[str, dict]] = []
    context = _context(
        company.id,
        uuid.uuid4(),
        session_factory,
        {
            "brand_id": str(brand.id),
                        "video_wait_seconds": 0,
        },
        stages,
    )
    result = await ContentAgentHandler().execute("content.production.run", context)

    assert result["project_status"] == "completed"
    assert monitored["called"] is False
    stage_names = [stage for stage, _details in stages]
    assert "monitoring.reused" in stage_names
    assert "monitoring.started" not in stage_names
