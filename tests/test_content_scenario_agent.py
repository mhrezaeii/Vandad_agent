"""Agent-driven scenario generation from verified platform knowledge."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.agents.runtime import AgentExecutionContext, ContentAgentHandler
from app.models import (
    BrandPlatformLink,
    ContentBrand,
    ContentProject,
    PlatformConnection,
    PlatformKnowledgeSnapshot,
    ProjectStatus,
)


async def test_content_agent_generates_scenario_from_latest_brand_knowledge_and_is_replay_safe(
    db_session, session_factory, company_and_platform, monkeypatch
):
    from app.services import content_scenario_service

    company, platform = company_and_platform
    brand = ContentBrand(
        company_id=company.id,
        name=f"brand-{uuid.uuid4().hex[:8]}",
        display_name="ونداد",
        description="سامانه هوشمند مدیریت و تولید محتوای تبلیغاتی",
        language="fa-IR",
        tone="حرفه‌ای و صمیمی",
        cta_config={"text": "بیشتر آشنا شوید", "hashtags": ["#ونداد"]},
        visual_config={"character_name": "وندیار"},
        status="active",
    )
    db_session.add(brand)
    await db_session.flush()

    project = ContentProject(
        company_id=company.id,
        brand_id=brand.id,
        topic="معرفی قابلیت‌های واقعی محصول",
        status=ProjectStatus.DRAFT.value,
        scene_options={"target_duration_seconds": 30, "scene_count": 5},
    )
    connection = PlatformConnection(
        company_id=company.id,
        platform_id=platform.id,
        name=f"llm-{uuid.uuid4().hex[:8]}",
        provider="llm_openai",
        status="connected",
        configuration={"url": "https://llm.example.test/v1", "model": "test-model"},
    )
    db_session.add_all([project, connection])
    db_session.add(BrandPlatformLink(
        company_id=company.id, brand_id=brand.id, platform_id=platform.id
    ))
    db_session.add(PlatformKnowledgeSnapshot(
        company_id=company.id,
        platform_id=platform.id,
        source_document_ids=[str(uuid.uuid4())],
        source_fingerprint="a" * 64,
        knowledge_json={
            "summary": "ونداد فرایند پایش و تولید محتوای تبلیغاتی را مدیریت می‌کند.",
            "capabilities": [{
                "name": "پایش پلتفرم",
                "description": "شناسایی تغییرات و استخراج اطلاعات محصول",
                "evidence": "پایش پلتفرم و استخراج اطلاعات",
                "source_url": "https://product.example.test/features",
            }],
            "products_and_services": [],
            "workflows": [],
            "target_audiences": [],
            "differentiators": [],
            "unknowns": ["قیمت‌گذاری عمومی مشخص نیست"],
        },
    ))
    await db_session.commit()

    captured = {"calls": 0, "request": None}
    scene_payload = {
        "index": 0,
        "narration": "محصولتان را بهتر معرفی کنید.",
        "visual_prompt": "A clean cinematic product interface shot",
        "duration_seconds": 30.0,
    }
    scenario_payload = {
        "title": "معرفی ونداد",
        "opening_hook": "محصولتان چقدر شناخته شده است؟",
        "scenes": [scene_payload],
        "narration": "محصولتان را بهتر معرفی کنید.",
        "cta": "بیشتر آشنا شوید",
        "caption": "معرفی ونداد",
        "hashtags": ["#ونداد"],
        "target_duration_seconds": 30,
        "total_duration_seconds": 30.0,
    }
    scenario = SimpleNamespace(
        model_dump=lambda mode: scenario_payload,
        title="معرفی ونداد",
        narration="محصولتان را بهتر معرفی کنید.",
        scenes=[SimpleNamespace(model_dump=lambda mode: scene_payload)],
        cta="بیشتر آشنا شوید",
        total_duration_seconds=30.0,
    )

    class FakeScenarioService:
        def __init__(self, session, registry, executor, master_key_box):
            self.session = session

        async def generate(self, *, company_id, connection_id, request, timeout_seconds):
            captured["calls"] += 1
            captured["request"] = request
            assert company_id == company.id
            assert connection_id == connection.id
            return scenario, {
                "model": "test-model",
                "usage": {"prompt_tokens": 100, "completion_tokens": 80, "total_tokens": 180},
            }

    monkeypatch.setattr(content_scenario_service, "ScenarioService", FakeScenarioService)

    context = AgentExecutionContext(
        company_id=company.id,
        task_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        worker_id="content-test-worker",
        payload={
            "project_id": str(project.id),
            "connection_id": str(connection.id),
        },
        session_factory=session_factory,
        provider_registry=object(),
        provider_executor=object(),
        master_key_box=object(),
    )
    handler = ContentAgentHandler()
    result = await handler.execute("content.scenario.generate", context)

    assert result["project_status"] == ProjectStatus.SCRIPT_READY.value
    assert result["approval_required"] is True
    assert result["scene_count"] == 1
    assert captured["request"].brand_name == "ونداد"
    assert captured["request"].character_name == "وندیار"
    assert "پایش پلتفرم" in captured["request"].platform_knowledge_context
    assert "قیمت‌گذاری عمومی مشخص نیست" in captured["request"].platform_knowledge_context
    assert captured["calls"] == 1

    # A redelivered task returns the persisted scenario without another LLM call.
    replay = await handler.execute("content.scenario.generate", context)
    assert replay["replayed"] is True
    assert replay["approval_required"] is True
    assert captured["calls"] == 1

    async with session_factory() as session:
        persisted = await session.get(ContentProject, project.id)
        assert persisted.status == ProjectStatus.SCRIPT_READY.value
        assert persisted.video_title == "معرفی ونداد"
        assert persisted.llm_usage["model"] == "test-model"
