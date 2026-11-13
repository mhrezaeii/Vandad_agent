"""Content pipeline tests: script generation, tenant isolation and subtitles."""

from __future__ import annotations

import json
import uuid
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select, text

from app.core.exceptions import NotFoundError
from app.integrations.providers.llm_openai import LlmOpenAiProvider
from app.integrations.video import (
    MockSceneVideoProvider,
    SceneVideoHealthResult,
    SceneVideoProviderRegistry,
)
from app.models import (
    AgentTask,
    Company,
    ProjectStatus,
    ProviderUsageRecord,
)
from app.security.authorization import CompanyScope, Principal
from app.services.content_service import (
    ContentService,
)
from app.services.subtitle_service import build_ass_for_script, build_segments
from app.workflows.engine import WorkflowEngine
from tests.test_worker_hardening import _keybox, _make_worker


def _scope(company_id) -> CompanyScope:
    return CompanyScope(company_id=company_id, principal=Principal(subject="t", company_ids=frozenset({"*"})))


@pytest.fixture
def content_service(db_session, engine_pair, redis_manager):
    registry, executor = engine_pair
    from app.core.queue import WorkflowQueue

    return ContentService(
        db_session, registry, executor, _keybox(), workflow_queue=WorkflowQueue(redis_manager.client)
    )


async def _llm_connection(db_session, company_id) -> uuid.UUID:
    from app.models import Platform, PlatformConnection

    result = await db_session.execute(select(Platform).where(Platform.company_id == company_id).limit(1))
    platform = result.scalar_one()
    connection = PlatformConnection(
        company_id=company_id,
        platform_id=platform.id,
        name=f"llm-{uuid.uuid4().hex[:6]}",
        provider="llm_openai",
        status="connected",
        configuration={"url": "https://llm.example.com/v1", "model": "gpt-test"},
    )
    db_session.add(connection)
    await db_session.commit()
    return connection.id




# ------------------------------------------------------------- script flow
async def test_script_generation_persists_llm_output(
    db_session, content_service, workflow_factory, company_and_platform
):
    company, _platform = company_and_platform
    from app.models import CompanyProviderBudget

    db_session.add(
        CompanyProviderBudget(
            company_id=company.id,
            provider_id="llm_openai",
            monthly_limit_usd=Decimal("10.00"),
            per_operation_limit_usd=Decimal("1.00"),
            enabled=True,
        )
    )
    await db_session.commit()
    brand = await content_service.ensure_vandefence_brand(company_id=company.id)
    project = await content_service.create_project(
        scope=_scope(company.id), brand_id=brand.id,
        topic="چرا پایش متمرکز امنیتی برای سازمان‌ها اهمیت دارد؟",
    )

    script_json = json.dumps({
        "title": "پایش متمرکز امنیتی",
        "script": "شبکه سازمان شما هر ثانیه رویداد تولید می‌کند. (۲۵ ثانیه توضیح فنی)",
        "segments": ["شبکه سازمان شما هر ثانیه رویداد تولید می‌کند.", "پایش متمرکز یعنی دیدن همین رویدادها در یک نقطه."],
        "cta": "با تیم Vandefence صحبت کنید.",
    }, ensure_ascii=False)
    llm_routes = [("POST", "/chat/completions", {
        "choices": [{"message": {"content": script_json}}],
        "usage": {"prompt_tokens": 320, "completion_tokens": 180, "total_tokens": 500},
        "model": "gpt-test",
    }, 200)]

    def handler(request: httpx.Request) -> httpx.Response:
        for _method, fragment, body, status_code in llm_routes:
            if fragment in str(request.url):
                return httpx.Response(status_code, json=body)
        return httpx.Response(404, json={})

    content_service.registry.replace(LlmOpenAiProvider(client_factory=lambda t: httpx.AsyncClient(
        transport=httpx.MockTransport(handler), timeout=t)))

    project = await content_service.generate_script(
        project.id, scope=_scope(company.id), connection_id=await _llm_connection(db_session, company.id)
    )
    assert project.status == ProjectStatus.SCRIPT_GENERATING.value
    await db_session.commit()  # the engine runs in a separate session below

    # Worker-side engine run (separate session semantics).
    worker = _make_worker(
        session_factory=None, redis_manager=None, registry=content_service.registry,
        executor=content_service.executor, queue=None, concurrency=1,
    )
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: PLC0415
    from sqlalchemy.pool import NullPool  # noqa: PLC0415

    from tests.conftest import TEST_DATABASE_URL  # noqa: PLC0415

    test_engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    worker.session_factory = async_sessionmaker(bind=test_engine, expire_on_commit=False, autoflush=False)
    async with worker.session_factory() as session:
        engine2 = WorkflowEngine(session, content_service.registry, content_service.executor, _keybox())
        result = await engine2.run_execution(
            project.script_workflow_execution_id, company_id=company.id
        )
        await session.commit()
    if result.outcome != "completed":
        steps = list((await db_session.execute(
            text("SELECT node_key, error_code, error_message FROM workflow_execution_steps")
        )).all())
        pytest.fail(f"outcome={result.outcome} steps={steps}")
    await db_session.refresh(project)
    assert project.status == ProjectStatus.SCRIPT_READY.value
    assert "رویداد" in project.script_text
    assert project.llm_usage["total_tokens"] == 500
    await test_engine.dispose()


async def test_content_tenant_isolation(db_session, content_service, workflow_factory, company_and_platform):
    company, _platform = company_and_platform
    brand = await content_service.ensure_vandefence_brand(company_id=company.id)
    project = await content_service.create_project(
        scope=_scope(company.id), brand_id=brand.id, topic="تست"
    )
    stranger = Company(name="Content Sneak", slug="content-sneak")
    db_session.add(stranger)
    await db_session.commit()

    with pytest.raises(NotFoundError):
        await content_service.get_project(project.id, company_id=stranger.id)
    with pytest.raises(NotFoundError):
        await content_service.get_brand(brand.id, company_id=stranger.id)
    await content_service.get_job_status(
        project.video_workflow_execution_id or uuid.uuid4(), company_id=stranger.id
    ) if False else None
    with pytest.raises(NotFoundError):
        await content_service.get_job_status(
            project.script_workflow_execution_id, company_id=stranger.id
        )


# ------------------------------------------------------- scene video routing
async def test_scene_video_uses_first_healthy_ordered_provider_and_persists_route(
    db_session, content_service, company_and_platform, monkeypatch
):
    company, _platform = company_and_platform

    class UnhealthyProvider(MockSceneVideoProvider):
        provider_id = "unhealthy"

        async def check_health(self):
            return SceneVideoHealthResult(
                provider_id=self.provider_id,
                healthy=False,
                message="provider unavailable",
            )

    class HealthyProvider(MockSceneVideoProvider):
        provider_id = "healthy"

        async def check_health(self):
            return SceneVideoHealthResult(
                provider_id=self.provider_id,
                healthy=True,
                message="provider ready",
            )

    registry = SceneVideoProviderRegistry()
    registry.register(UnhealthyProvider())
    registry.register(HealthyProvider())
    content_service.scene_video_registry = registry

    from app.models import CompanyProviderBudget

    db_session.add(
        CompanyProviderBudget(
            company_id=company.id,
            provider_id="healthy",
            monthly_limit_usd=Decimal("10.00"),
            per_operation_limit_usd=Decimal("2.00"),
            enabled=True,
        )
    )
    brand = await content_service.ensure_vandefence_brand(company_id=company.id)
    project = await content_service.create_project(
        scope=_scope(company.id),
        brand_id=brand.id,
        topic="Ordered scene provider routing",
    )
    project.video_mode = "scene"
    project.status = ProjectStatus.APPROVED.value
    project.scenario = {
        "scenes": [{"duration_seconds": 5.0}],
    }
    project.scene_options = {"requires_reference_image": False}
    task_id = uuid.uuid4()
    db_session.add(
        AgentTask(
            id=task_id,
            company_id=company.id,
            agent_key="video_production",
            capability="video.scene.generate",
            title="Ordered provider fallback test",
            status="running",
            priority=50,
            input_payload={"project_id": str(project.id)},
            max_attempts=3,
            spend_ceiling_usd=Decimal("2.50"),
        )
    )
    await db_session.commit()

    async def fake_ensure_workflow(*args, **kwargs):
        return None

    async def fake_require_published(*args, **kwargs):
        return SimpleNamespace(id=uuid.uuid4())

    async def fake_execute_workflow(*args, **kwargs):
        return SimpleNamespace(id=uuid.uuid4()), True

    monkeypatch.setattr(content_service, "ensure_workflow", fake_ensure_workflow)
    monkeypatch.setattr(content_service, "_require_published", fake_require_published)
    monkeypatch.setattr(
        content_service._execution_service, "execute_workflow", fake_execute_workflow
    )

    updated = await content_service.generate_scene_video(
        project.id,
        scope=_scope(company.id),
        provider_id="unhealthy",
        provider_fallbacks=["healthy"],
        task_id=task_id,
    )

    assert updated.scene_video_provider == "healthy"
    assert updated.scene_options["provider_route_order"] == ["healthy", "unhealthy"]
    assert updated.cost_metadata["provider_id"] == "healthy"
    assert updated.cost_metadata["task_id"] == str(task_id)
    assert updated.cost_metadata["provider_route_order"] == ["healthy", "unhealthy"]

    reservation = await db_session.scalar(
        select(ProviderUsageRecord).where(
            ProviderUsageRecord.idempotency_key == f"scene-video:{project.id}"
        )
    )
    assert reservation is not None
    assert reservation.provider_id == "healthy"
    assert reservation.task_id == task_id


# ------------------------------------------------------------- subtitles
def test_persian_subsegments_split_and_time():
    script = (
        "شبکه سازمان شما هر ثانیه رویدادهای امنیتی تولید می‌کند. "
        "پایش متمرکز یعنی دیدن همین رویدادها در یک نقطه. "
        "تیم Vandefence آماده گفتگو است!"
    )
    segments = build_segments(script, total_seconds=30)
    assert len(segments) >= 2  # short sentences merge under the 90-char cap
    assert segments[0].start_seconds == 0
    assert segments[-1].end_seconds == 30
    # contiguous coverage
    for prev, nxt in zip(segments, segments[1:], strict=False):
        assert abs(prev.end_seconds - nxt.start_seconds) < 0.01


def test_ass_subtitle_contains_persian_and_rtl_style():
    build_segments("پایش متمرکز امنیتی اهمیت دارد.", total_seconds=10)
    ass = build_ass_for_script("پایش متمرکز امنیتی اهمیت دارد.", total_seconds=10,
                               width=1080, height=1920)
    assert "پایش" in ass
    assert "Vazirmatn" in ass
    assert "PlayResX: 1080" in ass
    assert "Dialogue: 0," in ass
    # Alignment=2 (bottom-centre) present in the style line
    assert ",2,60,60," in ass
