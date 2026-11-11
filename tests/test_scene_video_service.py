"""SceneVideoService tests: preparation, idempotent submission, polling,
download, retry, quota exhaustion and cancellation. No real provider calls."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core.exceptions import ConflictError
from app.integrations.video import (
    MockSceneVideoProvider,
    SceneGenerationStatus,
    SceneJobState,
    SceneVideoPermanentError,
    SceneVideoQuotaExhaustedError,
    build_scene_video_registry,
)
from app.models import (
    ContentProject,
    ProviderQuotaUsage,
    ProviderUsageRecord,
    SceneVideoJob,
    SceneVideoJobStatus,
    WorkflowExecution,
)
from app.schemas.scenario import Scenario, SceneSpec
from app.security.authorization import CompanyScope, Principal
from app.services.scene_video_service import SceneVideoService, build_scene_provider_prompt


@pytest.fixture
def content_service(db_session, engine_pair, redis_manager):
    from app.core.queue import WorkflowQueue
    from app.services.content_service import ContentService
    from tests.test_worker_hardening import _keybox

    registry, executor = engine_pair
    return ContentService(
        db_session,
        registry,
        executor,
        _keybox(),
        workflow_queue=WorkflowQueue(redis_manager.client),
        scene_video_registry=build_scene_video_registry(),
    )


def _scenario(count: int = 3, duration: float = 5.0) -> Scenario:
    return Scenario(
        title="سناریوی تست",
        opening_hook="قلاب",
        scenes=[
            SceneSpec(
                index=index,
                narration=f"روایت {index}",
                visual_prompt=f"visual prompt {index}",
                duration_seconds=duration,
                on_screen_text=f"متن {index}",
                subtitle_text=f"زیرنویس {index}",
            )
            for index in range(count)
        ],
        narration=" ".join(f"روایت {index}" for index in range(count)),
        cta="شروع کنید",
        caption="کپشن",
        hashtags=["#تست"],
        target_duration_seconds=int(count * duration),
    )


async def _project(db_session, content_service, company_id) -> ContentProject:
    brand = await content_service.ensure_vandefence_brand(company_id=company_id)
    from app.security.authorization import CompanyScope, Principal

    scope = CompanyScope(
        company_id=company_id, principal=Principal(subject="t", company_ids=frozenset({"*"}))
    )
    project = await content_service.create_scene_project(
        scope=scope,
        brand_id=brand.id,
        topic="موضوع تست",
        target_duration_seconds=15,
    )
    await db_session.commit()
    return project


def _service(db_session, provider: MockSceneVideoProvider, **kwargs) -> SceneVideoService:
    registry = build_scene_video_registry(mock_provider=provider)
    return SceneVideoService(db_session, registry, **kwargs)


async def _jobs(db_session, project_id) -> list[SceneVideoJob]:
    return list(
        (
            await db_session.execute(
                select(SceneVideoJob)
                .where(SceneVideoJob.project_id == project_id)
                .order_by(SceneVideoJob.scene_index)
            )
        ).scalars()
    )


async def test_prepare_scenes_is_idempotent(
    db_session, content_service, company_and_platform
):
    company, _ = company_and_platform
    project = await _project(db_session, content_service, company.id)
    service = _service(db_session, MockSceneVideoProvider())

    first = await service.prepare_scenes(project, _scenario(), provider_id="mock")
    await db_session.commit()
    second = await service.prepare_scenes(project, _scenario(), provider_id="mock")
    await db_session.commit()

    assert len(first) == len(second) == 3
    assert [job.id for job in first] == [job.id for job in second]
    assert len(await _jobs(db_session, project.id)) == 3
    assert project.status == "scenes_prepared"
    assert project.scene_video_provider == "mock"
    assert project.scenario is not None


async def test_prepare_scenes_rejects_brand_presenter_without_lipsync_provider(
    db_session, content_service, company_and_platform
):
    company, _ = company_and_platform
    project = await _project(db_session, content_service, company.id)
    scenario = _scenario()
    scenario.scenes[1].scene_type = "presenter"
    scenario.scenes[1].speaker = "brand_character"
    scenario.scenes[1].dialogue = "سلام، من وندی هستم."
    service = _service(db_session, MockSceneVideoProvider())

    with pytest.raises(ConflictError, match="real image/audio lip-sync support"):
        await service.prepare_scenes(project, scenario, provider_id="mock")


async def test_submit_pending_never_submits_twice(
    db_session, content_service, company_and_platform
):
    company, _ = company_and_platform
    project = await _project(db_session, content_service, company.id)
    provider = MockSceneVideoProvider(poll_steps=1)
    service = _service(db_session, provider)

    await service.prepare_scenes(project, _scenario(), provider_id="mock")
    first = await service.submit_pending(project)
    await db_session.commit()
    second = await service.submit_pending(project)
    await db_session.commit()

    assert first["submitted"] == 3
    assert second["submitted"] == 0
    assert len(provider.jobs) == 3
    jobs = await _jobs(db_session, project.id)
    assert all(job.provider_job_id for job in jobs)
    assert project.status == "video_generating"


async def test_poll_and_retrieve_completed_scenes(
    db_session, content_service, company_and_platform, tmp_path
):
    from app.storage.local import LocalArtifactStorage

    company, _ = company_and_platform
    project = await _project(db_session, content_service, company.id)
    execution = WorkflowExecution(
        company_id=company.id,
        workflow_name="scene-test",
        workflow_type="video_production",
        status="running",
    )
    db_session.add(execution)
    await db_session.commit()

    provider = MockSceneVideoProvider(poll_steps=1)
    storage = LocalArtifactStorage(tmp_path / "artifacts", max_size_bytes=4 * 1024 * 1024)
    service = _service(db_session, provider, storage=storage)

    await service.prepare_scenes(project, _scenario(), provider_id="mock")
    await service.submit_pending(project)
    await db_session.commit()

    summary = await service.poll_jobs(project)
    await db_session.commit()
    assert summary["all_terminal"] is True
    assert summary["completed"] == 3
    assert project.status == "video_generated"

    retrieved = await service.retrieve_completed(project, execution_id=execution.id)
    await db_session.commit()
    assert retrieved["downloaded"] == 3
    jobs = await _jobs(db_session, project.id)
    assert all(job.status == SceneVideoJobStatus.DOWNLOADED.value for job in jobs)
    assert all(job.artifact_id is not None for job in jobs)
    assert all(job.cost_metadata["size_bytes"] > 0 for job in jobs)


async def test_completed_scene_jobs_settle_provider_usd_cost(
    db_session, content_service, company_and_platform
):
    from decimal import Decimal

    company, _ = company_and_platform
    project = await _project(db_session, content_service, company.id)
    reservation = ProviderUsageRecord(
        company_id=company.id,
        provider_id="mock",
        operation="content.scene.generate_video",
        status="reserved",
        idempotency_key=f"scene-cost-{project.id}",
        reserved_amount_usd=Decimal("5.00"),
        usage_payload={},
    )
    db_session.add(reservation)
    await db_session.flush()
    project.cost_metadata = {
        "provider_id": "mock",
        "budget_reservation_id": str(reservation.id),
        "status": "reserved",
    }

    provider = MockSceneVideoProvider(poll_steps=1)
    async def completed_with_cost(job_id):
        return SceneGenerationStatus(
            job_id=job_id,
            state=SceneJobState.COMPLETED.value,
            progress=1.0,
            raw={"cost_usd": "0.10", "currency": "USD"},
        )
    provider.get_generation_status = completed_with_cost
    service = _service(db_session, provider)

    await service.prepare_scenes(project, _scenario(), provider_id="mock")
    await service.submit_pending(project)
    await db_session.flush()
    summary = await service.poll_jobs(project)
    await db_session.commit()
    await db_session.refresh(reservation)

    assert summary["all_terminal"] is True
    assert reservation.status == "settled"
    assert reservation.actual_cost_usd == Decimal("0.30")
    assert project.cost_metadata["status"] == "settled"
    assert project.cost_metadata["actual_cost_usd"] == "0.30"


async def test_submission_failure_can_be_retried(
    db_session, content_service, company_and_platform
):
    company, _ = company_and_platform
    project = await _project(db_session, content_service, company.id)
    failing_scene = f"{project.id}:1"
    provider = MockSceneVideoProvider(poll_steps=1, fail_scene_ids={failing_scene})
    service = _service(db_session, provider)

    await service.prepare_scenes(project, _scenario(), provider_id="mock")
    with pytest.raises(SceneVideoPermanentError, match="mock provider was configured to fail"):
        await service.submit_pending(project)
    await db_session.commit()

    jobs = await _jobs(db_session, project.id)
    failed = [job for job in jobs if job.status == SceneVideoJobStatus.FAILED.value]
    assert len(failed) == 1
    assert failed[0].scene_index == 1
    assert failed[0].attempts == 1

    provider.fail_scene_ids.clear()
    retried = await service.submit_pending(project)
    await db_session.commit()
    assert retried["submitted"] == 1
    jobs = await _jobs(db_session, project.id)
    assert all(job.provider_job_id for job in jobs)


async def test_quota_exhaustion_is_reported_and_persisted(
    db_session, content_service, company_and_platform
):
    company, _ = company_and_platform
    project = await _project(db_session, content_service, company.id)
    provider = MockSceneVideoProvider(poll_steps=1, quota_limit_per_day=1)
    service = _service(db_session, provider)

    await service.prepare_scenes(project, _scenario(), provider_id="mock")
    with pytest.raises(SceneVideoQuotaExhaustedError):
        await service.submit_pending(project)
    await db_session.commit()

    usage = (
        await db_session.execute(
            select(ProviderQuotaUsage).where(ProviderQuotaUsage.company_id == company.id)
        )
    ).scalar_one()
    assert usage.units_used == 1
    assert usage.units_limit == 1


async def test_cancel_marks_pending_jobs_and_project(
    db_session, content_service, company_and_platform
):
    company, _ = company_and_platform
    project = await _project(db_session, content_service, company.id)
    provider = MockSceneVideoProvider(poll_steps=5)
    service = _service(db_session, provider)

    await service.prepare_scenes(project, _scenario(), provider_id="mock")
    await service.submit_pending(project)
    await db_session.commit()

    result = await service.cancel(project)
    await db_session.commit()

    assert result["cancelled"] == 3
    jobs = await _jobs(db_session, project.id)
    assert all(job.status == SceneVideoJobStatus.CANCELLED.value for job in jobs)
    assert project.status == "cancelled"


async def test_scene_project_is_tenant_isolated(
    db_session, content_service, company_and_platform
):
    from app.core.exceptions import NotFoundError
    from app.models import Company

    company, _ = company_and_platform
    project = await _project(db_session, content_service, company.id)
    stranger = Company(name="Stranger", slug="stranger")
    db_session.add(stranger)
    await db_session.commit()

    with pytest.raises(NotFoundError):
        await content_service.get_project(project.id, company_id=stranger.id)
    with pytest.raises(NotFoundError):
        await content_service.list_scene_jobs(
            project.id,
            scope=CompanyScope(
                company_id=stranger.id,
                principal=Principal(subject="x", company_ids=frozenset({"*"})),
            ),
        )



def test_scene_provider_prompt_carries_character_identity_dialogue_and_product_direction():
    presenter = SceneSpec(
        index=0,
        narration="سلام، من وندی هستم.",
        visual_prompt="A friendly brand presenter in a modern office.",
        duration_seconds=5,
        scene_type="presenter",
        speaker="brand_character",
        dialogue="سلام، من وندی هستم.",
        camera_direction="medium close-up, slow push-in",
        transition="match_cut",
    )
    scenario = Scenario(
        title="معرفی وندیار",
        opening_hook="همه ابزارها یک‌جا",
        scenes=[presenter, SceneSpec(
            index=1,
            narration="همه چیز یکپارچه است.",
            visual_prompt="A clean SaaS dashboard on a desktop monitor.",
            duration_seconds=5,
            scene_type="product_demo",
        ), SceneSpec(
            index=2,
            narration="همین حالا شروع کن.",
            visual_prompt="Brand end card with abstract motion shapes.",
            duration_seconds=5,
            scene_type="cta",
        )],
        narration="سلام، من وندی هستم. همه چیز یکپارچه است. همین حالا شروع کن.",
        target_duration_seconds=15,
        character_name="وندی",
        character_description="Short dark hair, glasses, navy outfit, same face in every shot.",
        visual_style="premium cinematic navy and teal brand film",
    )
    prompt = build_scene_provider_prompt(presenter, scenario)

    assert "SCENE TYPE: presenter" in prompt
    assert "CHARACTER NAME: وندی" in prompt
    assert "same face in every shot" in prompt
    assert "SPOKEN DIALOGUE" in prompt
    assert "medium close-up" in prompt
    assert "match_cut" in prompt
