"""Content pipeline tests: script generation, idempotent paid submission,
poll loop, download, tenant isolation, subtitles."""

from __future__ import annotations

import json
import uuid
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select, text

from app.core.exceptions import ConflictError, NotFoundError
from app.integrations.providers.heygen import HeyGenProvider
from app.integrations.providers.llm_openai import LlmOpenAiProvider
from app.models import (
    BrandCharacter,
    CharacterStatus,
    Company,
    ContentBrand,
    Platform,  # noqa: E402
    PlatformConnection,
    ProjectStatus,
)
from app.security.authorization import CompanyScope, Principal
from app.services.content_service import (
    ContentService,
)
from app.services.provider_budget_service import ProviderBudgetService
from app.services.subtitle_service import build_ass_for_script, build_segments
from app.workflows.engine import WorkflowEngine
from tests.test_worker_hardening import _keybox, _make_worker


async def _heygen_connection(db_session, company_id, *, with_credential: bool = True) -> uuid.UUID:
    result = await db_session.execute(select(Platform).where(Platform.company_id == company_id).limit(1))
    platform = result.scalar_one()
    connection = PlatformConnection(
        company_id=company_id,
        platform_id=platform.id,
        name=f"heygen-{uuid.uuid4().hex[:6]}",
        provider="heygen",
        status="connected",
        configuration={"url": "https://api.heygen.com"},
    )
    db_session.add(connection)
    await db_session.flush()
    if with_credential:
        from app.models import CredentialStorageType
        from app.security.secret_value import SecretValue
        from app.services.credential_service import CredentialService
        from tests.test_worker_hardening import _keybox

        await CredentialService(db_session, _keybox()).create_credential(
            platform_id=platform.id,
            credential_type="api_key",
            secret_reference=f"vault://heygen/{uuid.uuid4().hex[:8]}",
            storage_type=CredentialStorageType.ENCRYPTED,
            value=SecretValue("heygen-test-key"),
        )
        await db_session.flush()
        connection.credential_reference_id = (
            (await db_session.execute(
                select(__import__("app").models.CredentialReference).where(
                    __import__("app").models.CredentialReference.platform_id == platform.id
                )
            ))
            .scalars()
            .first()
        ).id
    await ProviderBudgetService(db_session).set_budget(
        company_id,
        "heygen",
        monthly_limit_usd=Decimal("50.00"),
        per_operation_limit_usd=Decimal("10.00"),
    )
    await db_session.commit()
    return connection.id


def _scope(company_id) -> CompanyScope:
    return CompanyScope(company_id=company_id, principal=Principal(subject="t", company_ids=frozenset({"*"})))


@pytest.fixture
def content_service(db_session, engine_pair, redis_manager):
    registry, executor = engine_pair
    from app.core.queue import WorkflowQueue

    return ContentService(
        db_session, registry, executor, _keybox(), workflow_queue=WorkflowQueue(redis_manager.client)
    )


async def _brand_with_character(
    db_session, content_service, company_id, *, active=True
) -> tuple[ContentBrand, BrandCharacter]:
    brand = await content_service.ensure_vandefence_brand(company_id=company_id)
    character = await content_service.get_character(brand.id, company_id=company_id)
    character.provider = "heygen"
    character.provider_avatar_id = "av-persian-77"
    character.provider_group_id = "grp-77"
    character.avatar_kind = "photo_avatar"
    character.voice_id = "voice-fa-1"
    character.status = CharacterStatus.ACTIVE.value if active else CharacterStatus.INACTIVE.value
    await db_session.commit()
    return brand, character


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
    brand, _character = await _brand_with_character(db_session, content_service, company.id)
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


async def test_approve_and_video_submission_is_idempotent(
    db_session, content_service, workflow_factory, company_and_platform, monkeypatch
):
    """The paid HeyGen submission happens exactly once, even across retries."""
    company, _platform = company_and_platform
    brand, _character = await _brand_with_character(db_session, content_service, company.id)
    project = await content_service.create_project(
        scope=_scope(company.id), brand_id=brand.id, topic="تست"
    )
    project.script_text = "اسکریپت تایید شده."
    project.status = ProjectStatus.SCRIPT_READY.value
    await db_session.commit()

    await content_service.approve_script(project.id, scope=_scope(company.id))
    await db_session.refresh(project)
    assert project.status == ProjectStatus.APPROVED.value

    submit_calls: list[int] = []

    def heygen_handler(request: httpx.Request) -> httpx.Response:
        if "/v3/videos" in str(request.url) and request.method == "POST":
            submit_calls.append(1)
            return httpx.Response(200, json={"video_id": "hg-paid-1"})
        if "/v3/videos/hg-paid-1" in str(request.url):
            return httpx.Response(200, json={"data": {
                "status": "processing"}})
        return httpx.Response(404, json={})

    content_service.registry.replace(HeyGenProvider(client_factory=lambda t: httpx.AsyncClient(
        transport=httpx.MockTransport(heygen_handler), timeout=t)))

    heygen_conn = await _heygen_connection(db_session, company.id)
    project = await content_service.generate_video(
        project.id, scope=_scope(company.id), connection_id=heygen_conn
    )
    assert project.status == ProjectStatus.VIDEO_GENERATING.value
    await db_session.commit()  # separate-session engine run below

    # Worker engine run; the poll node raises still_processing (transient).
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: PLC0415
    from sqlalchemy.pool import NullPool  # noqa: PLC0415

    from tests.conftest import TEST_DATABASE_URL  # noqa: PLC0415

    test_engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    session_factory = async_sessionmaker(bind=test_engine, expire_on_commit=False, autoflush=False)
    async with session_factory() as session:
        engine = WorkflowEngine(session, content_service.registry, content_service.executor,
                                _keybox())
        result = await engine.run_execution(
            project.video_workflow_execution_id, company_id=company.id
        )
        await session.commit()
    # First run: submit completed (persisted job id), wait node deferred.
    if result.outcome != "waiting":
        steps = list((await db_session.execute(
            text("SELECT node_key, status, error_code, error_message FROM workflow_execution_steps")
        )).all())
        pytest.fail(f"first-run outcome={result.outcome} steps={steps}")
    await db_session.refresh(project)
    if project.provider_job_id != "hg-paid-1":
        steps = list((await db_session.execute(
            text("SELECT node_key, status, error_code, error_message FROM workflow_execution_steps")
        )).all())
        pytest.fail(f"provider_job_id={project.provider_job_id} steps={steps}")
    assert project.provider_job_id == "hg-paid-1"
    assert len(submit_calls) == 1  # exactly ONE paid submission

    # Worker restart mid-wait: the run resumes, poll raises still_processing,
    # and the submit node must NOT resubmit. A short execution deadline makes
    # the poll retry loop end quickly (timed_out).
    execution_row = await db_session.get(
        __import__("app").models.WorkflowExecution, project.video_workflow_execution_id
    )
    execution_row.timeout_seconds = 2
    await db_session.commit()
    async with session_factory() as session:
        engine = WorkflowEngine(session, content_service.registry, content_service.executor, _keybox())
        result2 = await engine.run_execution(
            project.video_workflow_execution_id, company_id=company.id
        )
        await session.commit()
    assert result2.outcome in ("timed_out", "failed")
    assert len(submit_calls) == 1  # still exactly one — no duplicate billing
    await test_engine.dispose()


async def test_video_generation_requires_active_character(
    db_session, content_service, workflow_factory, company_and_platform
):
    company, _platform = company_and_platform
    brand, _character = await _brand_with_character(
        db_session, content_service, company.id, active=False
    )
    project = await content_service.create_project(
        scope=_scope(company.id), brand_id=brand.id, topic="تست"
    )
    project.script_text = "متن"
    project.status = ProjectStatus.APPROVED.value
    await db_session.commit()

    with pytest.raises(ConflictError):
        await content_service.generate_video(
            project.id, scope=_scope(company.id), connection_id=uuid.uuid4()
        )


async def test_generate_video_requires_approval(
    db_session, content_service, workflow_factory, company_and_platform
):
    company, _platform = company_and_platform
    brand, _character = await _brand_with_character(db_session, content_service, company.id)
    project = await content_service.create_project(
        scope=_scope(company.id), brand_id=brand.id, topic="تست"
    )
    with pytest.raises(ConflictError):
        await content_service.generate_video(
            project.id, scope=_scope(company.id), connection_id=uuid.uuid4()
        )


async def test_content_tenant_isolation(db_session, content_service, workflow_factory, company_and_platform):
    company, _platform = company_and_platform
    brand, _character = await _brand_with_character(db_session, content_service, company.id)
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
