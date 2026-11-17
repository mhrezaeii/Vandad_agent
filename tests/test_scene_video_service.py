"""SceneVideoService tests: preparation, idempotent submission, polling,
download, retry, quota exhaustion and cancellation. No real provider calls."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.core.exceptions import ConflictError
from app.integrations.video import (
    MockSceneVideoProvider,
    SceneGenerationStatus,
    SceneJobState,
    SceneVideoPermanentError,
    SceneVideoQuotaExhaustedError,
    SceneVideoRequest,
    SceneVideoTransientError,
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


def _single_scene_scenario(duration: float = 5.0) -> Scenario:
    """Build a one-job fixture for the single-clip passthrough mode."""
    scenario = _scenario()
    return scenario.model_copy(
        update={"scenes": scenario.scenes[:1], "target_duration_seconds": int(duration)}
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



async def test_single_scene_project_finalizes_as_provider_passthrough(
    db_session, content_service, company_and_platform, tmp_path
):
    from app.storage.local import LocalArtifactStorage

    class GenerationOnlyMockProvider(MockSceneVideoProvider):
        provider_id = "genonly"
        supports_composition = False
        supports_quality_review = False

    company, _ = company_and_platform
    project = await _project(db_session, content_service, company.id)
    execution = WorkflowExecution(
        company_id=company.id,
        workflow_name="single-scene-passthrough-test",
        workflow_type="video_production",
        status="running",
    )
    db_session.add(execution)
    await db_session.commit()

    provider = GenerationOnlyMockProvider(poll_steps=1)
    storage = LocalArtifactStorage(tmp_path / "artifacts", max_size_bytes=4 * 1024 * 1024)
    service = _service(db_session, provider, storage=storage)

    await service.prepare_scenes(
        project, _single_scene_scenario(), provider_id="genonly"
    )
    await service.submit_pending(project)
    await db_session.commit()
    summary = await service.poll_jobs(project)
    assert summary["completed"] == 1
    retrieved = await service.retrieve_completed(project, execution_id=execution.id)
    await db_session.commit()
    assert retrieved["downloaded"] == 1

    artifact, metadata = await service.finalize_project_video(
        project, execution_id=execution.id, width=1080, height=1920
    )
    await db_session.commit()

    assert project.status == "completed"
    assert project.final_artifact_id == artifact.id
    assert metadata["mode"] == "single_scene_passthrough"
    assert metadata["production_path"] == "provider_single_scene"
    assert metadata["provider"] == "genonly"
    assert metadata["scene_artifact_id"] == str(artifact.id)
    assert metadata["quality_review"]["status"] == "not_applicable"
    # Only the scene generation job exists; no composition job was submitted.
    assert len(provider.jobs) == 1
    assert artifact.content_type == "video/mp4"
    assert artifact.size_bytes > 0


async def test_single_scene_passthrough_settles_budget_reservation(
    db_session, content_service, company_and_platform, tmp_path
):
    from decimal import Decimal

    from app.storage.local import LocalArtifactStorage

    class GenerationOnlyMockProvider(MockSceneVideoProvider):
        provider_id = "genonly"
        supports_composition = False
        supports_quality_review = False

    company, _ = company_and_platform
    project = await _project(db_session, content_service, company.id)
    reservation = ProviderUsageRecord(
        company_id=company.id,
        provider_id="genonly",
        operation="content.scene.generate_video",
        status="reserved",
        idempotency_key=f"single-scene-cost-{project.id}",
        reserved_amount_usd=Decimal("0.10"),
        usage_payload={},
    )
    db_session.add(reservation)
    await db_session.flush()
    project.cost_metadata = {
        "provider_id": "genonly",
        "budget_reservation_id": str(reservation.id),
        "status": "reserved",
    }
    execution = WorkflowExecution(
        company_id=company.id,
        workflow_name="single-scene-budget-test",
        workflow_type="video_production",
        status="running",
    )
    db_session.add(execution)
    await db_session.commit()

    provider = GenerationOnlyMockProvider(poll_steps=1)
    storage = LocalArtifactStorage(tmp_path / "artifacts", max_size_bytes=4 * 1024 * 1024)
    service = _service(db_session, provider, storage=storage)
    await service.prepare_scenes(
        project, _single_scene_scenario(), provider_id="genonly"
    )
    await service.submit_pending(project)
    await service.poll_jobs(project)
    await service.retrieve_completed(project, execution_id=execution.id)
    jobs = await _jobs(db_session, project.id)
    assert len(jobs) == 1
    # Simulate the provider reporting the billed amount for the single clip.
    jobs[0].cost_metadata = {**(jobs[0].cost_metadata or {}), "reported_cost_usd": "0.10"}
    await db_session.commit()

    await service.finalize_project_video(
        project, execution_id=execution.id, width=1080, height=1920
    )
    await db_session.commit()
    await db_session.refresh(reservation)
    await db_session.refresh(project)

    assert reservation.status == "settled"
    assert reservation.actual_cost_usd == Decimal("0.10")
    assert project.cost_metadata["status"] == "settled"
    assert project.cost_metadata["actual_cost_usd"] == "0.10"


async def test_final_video_is_composed_by_ai_provider_and_retried_idempotently(
    db_session, content_service, company_and_platform, tmp_path
):
    from app.storage.local import LocalArtifactStorage

    company, _ = company_and_platform
    project = await _project(db_session, content_service, company.id)
    execution = WorkflowExecution(
        company_id=company.id,
        workflow_name="ai-compose-test",
        workflow_type="video_production",
        status="running",
    )
    db_session.add(execution)
    await db_session.commit()

    provider = MockSceneVideoProvider(poll_steps=1)
    storage = LocalArtifactStorage(tmp_path / "artifacts", max_size_bytes=4 * 1024 * 1024)
    service = _service(db_session, provider, storage=storage)
    await service.prepare_scenes(project, _scenario(), provider_id="mock")
    jobs = await _jobs(db_session, project.id)
    for job in jobs:
        job.video_url = f"https://provider.example/{job.scene_key}.mp4"
    await db_session.commit()

    with pytest.raises(SceneVideoTransientError, match="submitted"):
        await service.finalize_project_video(
            project,
            execution_id=execution.id,
            width=1080,
            height=1920,
            brand_context={"name": "Vandad"},
        )
    persisted_job_id = project.assembly_metadata["composition_job_id"]
    assert persisted_job_id
    assert project.provider_job_id is None
    assert project.submission_metadata is None
    await db_session.commit()

    artifact, metadata = await service.finalize_project_video(
        project,
        execution_id=execution.id,
        width=1080,
        height=1920,
        brand_context={"name": "Vandad"},
    )
    await db_session.commit()

    assert project.status == "completed"
    assert project.final_artifact_id == artifact.id
    assert metadata["service"] == "ai_provider"
    assert metadata["mode"] == "ai_composition"
    assert metadata["provider_job_id"] == persisted_job_id
    assert metadata["production_path"] == "ai_provider_composition"
    assert metadata["quality_review"]["accepted"] is True
    assert "no visual/audio quality is assessed" in metadata["quality_review"]["summary"]
    assert project.provider_job_id is None
    assert artifact.content_type == "video/mp4"
    assert artifact.size_bytes > 0
    assert len(provider.jobs) == 1

    same_artifact, _ = await service.finalize_project_video(
        project,
        execution_id=execution.id,
        width=1080,
        height=1920,
        brand_context={"name": "Vandad"},
    )
    assert same_artifact.id == artifact.id
    assert len(provider.jobs) == 1




async def test_scene_video_budget_waits_for_composition_and_aggregates_cost(
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
    assert reservation.status == "reserved"
    assert reservation.actual_cost_usd is None
    assert project.cost_metadata["status"] == "reserved"

    await service._settle_project_video_budget(
        project,
        await _jobs(db_session, project.id),
        composition_cost_usd=Decimal("0.20"),
        quality_cost_usd=Decimal("0.05"),
    )
    await db_session.commit()
    await db_session.refresh(reservation)

    assert reservation.status == "settled"
    assert reservation.actual_cost_usd == Decimal("0.55")
    assert project.cost_metadata["status"] == "settled"
    assert project.cost_metadata["actual_cost_usd"] == "0.55"


async def test_scene_video_budget_stays_reserved_when_composition_cost_is_unknown(
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
        idempotency_key=f"scene-cost-unknown-{project.id}",
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
    await service.poll_jobs(project)
    await service._settle_project_video_budget(
        project,
        await _jobs(db_session, project.id),
        composition_cost_usd=Decimal("0.20"),
        quality_cost_usd=None,
    )
    await db_session.commit()
    await db_session.refresh(reservation)

    assert reservation.status == "reserved"
    assert reservation.actual_cost_usd is None


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



def test_scene_provider_prompt_contains_product_direction_without_character_dependency():
    product = SceneSpec(
        index=0,
        narration="همه ابزارهای کاری یکپارچه در یک محیط.",
        visual_prompt="A clean SaaS dashboard on a desktop monitor.",
        duration_seconds=5,
        scene_type="product_demo",
        camera_direction="slow cinematic push-in",
        transition="match_cut",
    )
    scenario = Scenario(
        title="معرفی ونداد",
        opening_hook="همه ابزارها یک‌جا",
        scenes=[
            product,
            SceneSpec(
                index=1,
                narration="فرایندها را ساده‌تر مدیریت کنید.",
                visual_prompt="Abstract workflow graphics connecting business tools.",
                duration_seconds=5,
                scene_type="motion_graphic",
            ),
            SceneSpec(
                index=2,
                narration="همین حالا شروع کن.",
                visual_prompt="Minimal brand end card with abstract motion shapes.",
                duration_seconds=5,
                scene_type="cta",
            ),
        ],
        narration="همه ابزارهای کاری یکپارچه در یک محیط. فرایندها را ساده‌تر مدیریت کنید. همین حالا شروع کن.",
        target_duration_seconds=15,
        visual_style="premium cinematic navy and teal brand film",
    )
    prompt = build_scene_provider_prompt(product, scenario)

    assert "SCENE TYPE: product_demo" in prompt
    assert "PRODUCT DEMO" in prompt
    assert "slow cinematic push-in" in prompt
    assert "match_cut" in prompt
    assert "CHARACTER" not in prompt
    assert "PRESENTER" not in prompt


async def test_final_video_is_rejected_when_ai_quality_review_fails(
    db_session, content_service, company_and_platform, tmp_path
):
    from app.integrations.video import SceneVideoQualityResult
    from app.storage.local import LocalArtifactStorage

    class RejectingMockProvider(MockSceneVideoProvider):
        async def review_video(self, request):
            return SceneVideoQualityResult(
                accepted=False,
                score=0.2,
                summary="The final video failed the quality gate.",
                issues=("Narration mismatch",),
            )

    company, _ = company_and_platform
    project = await _project(db_session, content_service, company.id)
    execution = WorkflowExecution(
        company_id=company.id,
        workflow_name="quality-rejection-test",
        workflow_type="video_production",
        status="running",
    )
    db_session.add(execution)
    await db_session.commit()
    provider = RejectingMockProvider(poll_steps=1)
    storage = LocalArtifactStorage(tmp_path / "artifacts", max_size_bytes=4 * 1024 * 1024)
    service = _service(db_session, provider, storage=storage)
    await service.prepare_scenes(project, _scenario(), provider_id="mock")
    for job in await _jobs(db_session, project.id):
        job.video_url = f"https://provider.example/{job.scene_key}.mp4"
    await db_session.commit()

    with pytest.raises(SceneVideoTransientError, match="submitted"):
        await service.finalize_project_video(
            project, execution_id=execution.id, width=1080, height=1920
        )
    await db_session.commit()

    with pytest.raises(SceneVideoPermanentError, match="rejected final-video quality"):
        await service.finalize_project_video(
            project, execution_id=execution.id, width=1080, height=1920
        )
    await db_session.commit()

    assert project.status == "failed"
    assert project.final_artifact_id is None
    assert project.assembly_metadata["quality_review"]["accepted"] is False


async def test_submit_pending_blocks_paid_generation_when_live_health_probe_is_unhealthy(
    db_session, content_service, company_and_platform
):
    from app.integrations.video import SceneVideoHealthResult

    class UnhealthyMockProvider(MockSceneVideoProvider):
        async def check_health(self) -> SceneVideoHealthResult:
            return SceneVideoHealthResult(
                provider_id=self.provider_id,
                healthy=False,
                message="Provider is unavailable.",
            )

    company, _ = company_and_platform
    project = await _project(db_session, content_service, company.id)
    provider = UnhealthyMockProvider()
    service = _service(db_session, provider)
    await service.prepare_scenes(project, _scenario(), provider_id="mock")

    with pytest.raises(SceneVideoTransientError, match="paid submission was not started"):
        await service.submit_pending(project)

    assert provider.jobs == {}
    jobs = await _jobs(db_session, project.id)
    assert all(job.attempts == 0 for job in jobs)



async def test_ambiguous_provider_submit_is_marked_unknown_and_never_retried(
    db_session, content_service, company_and_platform
):
    from app.integrations.video import SceneVideoTransientError

    company, _ = company_and_platform
    project = await _project(db_session, content_service, company.id)
    provider = MockSceneVideoProvider()
    calls = 0

    async def ambiguous_submit(_request):
        nonlocal calls
        calls += 1
        raise SceneVideoTransientError(
            "Submission outcome unknown.", code="provider_submission_unknown"
        )

    provider.generate_video = ambiguous_submit
    service = _service(db_session, provider)
    await service.prepare_scenes(project, _single_scene_scenario(), provider_id="mock")

    with pytest.raises(SceneVideoTransientError):
        await service.submit_pending(project)

    job = (await _jobs(db_session, project.id))[0]
    assert calls == 1
    assert job.status == SceneVideoJobStatus.FAILED.value
    assert job.attempts == job.max_attempts
    assert job.submission_metadata["submission_outcome"] == "unknown"
    assert job.submission_metadata["reconcile_before_retry"] is True
    await db_session.commit()

    result = await service.submit_pending(project)
    assert result["submitted"] == 0
    assert calls == 1



async def test_recent_wavespeed_submit_fence_skips_second_worker(
    db_session, content_service, company_and_platform
):
    company, _ = company_and_platform
    project = await _project(db_session, content_service, company.id)
    provider = MockSceneVideoProvider()
    provider.provider_id = "wavespeed"
    service = _service(db_session, provider)
    await service.prepare_scenes(project, _single_scene_scenario(), provider_id="wavespeed")
    job = (await _jobs(db_session, project.id))[0]
    job.attempts = 1
    job.submission_metadata = {
        "submission_outcome": "submitting",
        "submission_started_at": datetime.now(UTC).isoformat(),
    }
    await db_session.commit()

    result = await service.submit_pending(project)
    assert result["submitted"] == 0
    assert provider.jobs == {}
    assert job.status == SceneVideoJobStatus.PENDING.value


async def test_stale_wavespeed_submit_fence_becomes_unknown_without_resubmit(
    db_session, content_service, company_and_platform
):
    company, _ = company_and_platform
    project = await _project(db_session, content_service, company.id)
    provider = MockSceneVideoProvider()
    provider.provider_id = "wavespeed"
    service = _service(db_session, provider)
    await service.prepare_scenes(project, _single_scene_scenario(), provider_id="wavespeed")
    job = (await _jobs(db_session, project.id))[0]
    job.attempts = 1
    job.submission_metadata = {
        "submission_outcome": "submitting",
        "submission_started_at": (datetime.now(UTC) - timedelta(minutes=10)).isoformat(),
    }
    await db_session.commit()

    with pytest.raises(SceneVideoPermanentError) as error:
        await service.submit_pending(project)
    assert error.value.code == "provider_submission_unknown"
    assert provider.jobs == {}
    assert job.status == SceneVideoJobStatus.FAILED.value
    assert job.attempts == job.max_attempts
    assert job.submission_metadata["reconcile_before_retry"] is True


def test_overlay_planner_maps_screenshots_to_scene_windows(tmp_path):
    """Real-UI screenshots map to their scene windows; subtitles are planned."""
    from types import SimpleNamespace

    from app.services.scene_video_service import SceneVideoService

    platform_dir = tmp_path / "vandiyar"
    platform_dir.mkdir()
    shots = []
    for name in ("01.png", "02.png"):
        item = platform_dir / name
        item.write_bytes(b"\x89PNG fake")
        shots.append(item)

    service = SceneVideoService.__new__(SceneVideoService)
    service._screenshots_root = tmp_path
    project = SimpleNamespace(
        scene_options={"screenshots_platform": "vandiyar"},
        script_text="روایت اول. روایت دوم.",
    )
    jobs = [
        SimpleNamespace(scene_index=0, duration_seconds=15.0),
        SimpleNamespace(scene_index=1, duration_seconds=15.0),
    ]
    overlays, subtitle_path = service._plan_real_product_overlays(
        project, jobs, output_dir_hint=__import__("uuid").uuid4(), width=1080, height=1920,
    )
    assert [(str(o[0]), round(o[1], 3), round(o[2], 3)) for o in overlays] == [
        (str(shots[0]), 0.0, 15.0),
        (str(shots[1]), 15.0, 30.0),
    ]
    assert subtitle_path is not None and subtitle_path.exists()
    assert "Dialogue" in subtitle_path.read_text(encoding="utf-8")


def test_overlay_planner_skips_without_platform_or_screenshots(tmp_path):
    from types import SimpleNamespace

    from app.services.scene_video_service import SceneVideoService

    service = SceneVideoService.__new__(SceneVideoService)
    service._screenshots_root = tmp_path
    project = SimpleNamespace(scene_options={"screenshots_platform": "unknown"}, script_text="")
    jobs = [SimpleNamespace(scene_index=0, duration_seconds=15.0)]
    overlays, subtitle_path = service._plan_real_product_overlays(
        project, jobs, output_dir_hint=__import__("uuid").uuid4(), width=1080, height=1920,
    )
    assert overlays == [] and subtitle_path is None


def test_narration_fit_and_mux_arg_builders():
    from pathlib import Path

    from app.services.video_assembly import (
        build_audio_concat_args,
        build_narration_mux_args,
        build_silence_args,
    )

    mux = build_narration_mux_args(
        ffmpeg="ffmpeg", video_path=Path("v.mp4"), narration_audio_path=Path("n.wav"),
        output_path=Path("o.mp4"),
    )
    assert "-c:v" in mux and "copy" in mux and "-shortest" in mux
    silence = build_silence_args(ffmpeg="ffmpeg", seconds=15.0, output_path=Path("s.wav"))
    assert "anullsrc" in " ".join(silence) and "15.000" in " ".join(silence)
    concat = build_audio_concat_args(ffmpeg="ffmpeg", concat_list=Path("l.txt"), output_path=Path("n.wav"))
    assert "-f" in concat and "concat" in concat


def test_flow_quota_tracker_and_cookie_conversion(tmp_path):
    from app.services.flow_generation_service import (
        FlowError,
        FlowGenerationService,
        QuotaTracker,
        convert_cookie_export,
    )

    state = tmp_path / "storage_state.json"
    tracker = QuotaTracker(state, daily_limit=3)
    assert tracker.remaining() == 3
    tracker.consume()
    tracker.consume()
    assert tracker.used_today() == 2 and tracker.remaining() == 1

    export = [
        {"name": "__Secure-1PSID", "value": "abc", "domain": ".google.com", "path": "/",
         "httpOnly": True, "secure": True, "sameSite": "no_restriction", "expirationDate": 1893456000},
        {"name": "NID", "value": "x", "domain": ".google.com", "path": "/", "secure": True},
        {"name": "other", "value": "y", "domain": ".example.com", "path": "/", "secure": True},
    ]
    out = convert_cookie_export(export, output_path=tmp_path / "state.json")
    cookies = json.loads(out.read_text(encoding="utf-8"))["cookies"]
    assert len(cookies) == 2
    psid = next(c for c in cookies if c["name"] == "__Secure-1PSID")
    assert psid["sameSite"] in ("Strict", "None", "Lax")
    assert all(c["domain"].endswith("google.com") for c in cookies)

    with __import__("pytest").raises(FlowError):
        convert_cookie_export([{"name": "x", "value": "y", "domain": ".example.com"}],
                              output_path=tmp_path / "empty.json")

    service = FlowGenerationService(__import__("types").SimpleNamespace(
        enabled=True, daily_limit=3, storage_state=state, profile_dir=tmp_path,
        chromium_path=tmp_path / "no-chromium", headless=True, timeout_seconds=60,
    ))
    status = service.status()
    assert status["session_present"] is False and status["remaining_today"] == 1  # same tracker state


# ------------------------------------------------------------ single-shot mode
class _ReferenceImageMock(MockSceneVideoProvider):
    """Mock provider exposing the reference-image + duration capability flags.

    Mirrors the KIE image-to-video fail-closed rule: no reference image means
    no paid submission.
    """

    provider_id = "mockref"
    supports_reference_image = True
    supports_native_audio = True
    min_duration_seconds = 6
    max_duration_seconds = 30
    max_prompt_characters = 5000

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.requests: list[SceneVideoRequest] = []

    async def generate_video(self, request: SceneVideoRequest) -> str:
        if not request.reference_image_url:
            raise SceneVideoPermanentError(
                "The mock image-to-video provider requires a character reference image.",
                code="provider_reference_image_missing",
            )
        self.requests.append(request)
        return await super().generate_video(request)


class _UploadingReferenceMock(_ReferenceImageMock):
    """Mock provider that also hosts local character images like KIE."""

    provider_id = "mockupload"

    def __init__(self, *, hosted_url: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self.hosted_url = hosted_url
        self.uploads: list[tuple[bytes, str]] = []

    async def upload_reference_image(self, data: bytes, *, file_name: str) -> str:
        self.uploads.append((data, file_name))
        return self.hosted_url


async def _single_shot_project(db_session, content_service, company_id, **options) -> ContentProject:
    brand = await content_service.ensure_vandefence_brand(company_id=company_id)
    scope = CompanyScope(
        company_id=company_id, principal=Principal(subject="t", company_ids=frozenset({"*"}))
    )
    project = await content_service.create_scene_project(
        scope=scope,
        brand_id=brand.id,
        topic="تک‌برداشت وندیار",
        target_duration_seconds=20,
        generation_mode="single_shot",
        **options,
    )
    await db_session.commit()
    return project


async def test_single_shot_prepares_exactly_one_job_for_a_multi_beat_scenario(
    db_session, content_service, company_and_platform
):
    company, _ = company_and_platform
    project = await _single_shot_project(
        db_session, content_service, company.id,
        character_key="Vandiyar/Emerald Blazer",
        character_description="young office worker, round glasses, red tie",
    )
    service = _service(db_session, _ReferenceImageMock())

    scenario = _scenario(count=4, duration=5.0)
    jobs = await service.prepare_scenes(project, scenario, provider_id="mockref")
    await db_session.commit()

    assert len(jobs) == 1
    assert len(await _jobs(db_session, project.id)) == 1
    job = jobs[0]
    assert job.scene_key == "single-shot"
    assert job.duration_seconds == 20.0
    assert job.narration == scenario.narration
    # The single prompt carries every beat, the Persian speech rules and the
    # character consistency instructions.
    prompt = job.prompt
    assert prompt.count("Beat ") == 4
    assert "Iranian Persian" in prompt
    assert "CHARACTER CONSISTENCY" in prompt
    assert job.submission_metadata["generation_mode"] == "single_shot"


async def test_single_shot_rejects_scenario_outside_provider_duration_bounds(
    db_session, content_service, company_and_platform
):
    from app.integrations.video.base import SceneVideoPermanentError

    company, _ = company_and_platform
    project = await _single_shot_project(
        db_session, content_service, company.id, character_key="Vandiyar/Emerald Blazer"
    )
    service = _service(db_session, _ReferenceImageMock())

    with pytest.raises(SceneVideoPermanentError) as error:
        await service.prepare_scenes(project, _scenario(count=10, duration=5.0), provider_id="mockref")
    assert error.value.code == "provider_invalid_duration"
    assert "never splits" in error.value.message
    assert await _jobs(db_session, project.id) == []

    project_short = await _single_shot_project(
        db_session, content_service, company.id, character_key="Vandiyar/Emerald Blazer"
    )
    with pytest.raises(SceneVideoPermanentError) as error:
        await service.prepare_scenes(
            project_short, _scenario(count=1, duration=5.0), provider_id="mockref"
        )
    assert error.value.code == "provider_invalid_duration"


async def test_single_shot_submit_passes_reference_image_and_submits_once(
    db_session, content_service, company_and_platform
):
    company, _ = company_and_platform
    project = await _single_shot_project(
        db_session, content_service, company.id,
        character_image_url="https://assets.example.test/character.png",
    )
    provider = _ReferenceImageMock(poll_steps=1)
    service = _service(db_session, provider)

    await service.prepare_scenes(project, _scenario(count=4, duration=5.0), provider_id="mockref")
    await db_session.commit()
    summary = await service.submit_pending(project)
    await db_session.commit()

    assert summary["submitted"] == 1
    assert len(provider.requests) == 1
    assert provider.requests[0].reference_image_url == "https://assets.example.test/character.png"
    assert provider.requests[0].metadata["generation_mode"] == "single_shot"
    # A second submission attempt is a no-op: no duplicate paid job.
    second = await service.submit_pending(project)
    assert second["submitted"] == 0
    assert len(provider.requests) == 1


async def test_single_shot_local_character_image_is_uploaded_and_transmitted(
    db_session, content_service, company_and_platform, tmp_path
):
    company, _ = company_and_platform
    character_root = tmp_path / "Paltform_charactar" / "Vandiyar"
    character_root.mkdir(parents=True)
    (character_root / "Emerald Blazer.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 1100)

    project = await _single_shot_project(
        db_session, content_service, company.id, character_key="Vandiyar/Emerald Blazer"
    )
    provider = _UploadingReferenceMock(
        hosted_url="https://tempfile.example.test/vandad/character-references/Emerald Blazer.png",
        poll_steps=1,
    )
    service = _service(db_session, provider, characters_root=tmp_path / "Paltform_charactar")

    await service.prepare_scenes(project, _scenario(count=3, duration=5.0), provider_id="mockupload")
    await db_session.commit()
    summary = await service.submit_pending(project)
    await db_session.commit()

    assert summary["submitted"] == 1
    assert len(provider.uploads) == 1
    data, file_name = provider.uploads[0]
    assert data.startswith(b"\x89PNG") and file_name == "Emerald Blazer.png"
    assert provider.requests[0].reference_image_url == provider.hosted_url


async def test_single_shot_missing_character_image_fails_before_paid_submission(
    db_session, content_service, company_and_platform
):
    company, _ = company_and_platform
    project = await _single_shot_project(db_session, content_service, company.id)
    provider = _ReferenceImageMock(poll_steps=1)
    service = _service(db_session, provider)

    await service.prepare_scenes(project, _scenario(count=3, duration=5.0), provider_id="mockref")
    await db_session.commit()
    with pytest.raises(SceneVideoPermanentError) as error:
        await service.submit_pending(project)
    assert error.value.code == "provider_reference_image_missing"
    assert provider.requests == []
    jobs = await _jobs(db_session, project.id)
    assert jobs[0].status == "failed"
    assert jobs[0].error_code == "provider_reference_image_missing"


async def test_multi_scene_mode_still_prepares_one_job_per_beat(
    db_session, content_service, company_and_platform
):
    company, _ = company_and_platform
    brand = await content_service.ensure_vandefence_brand(company_id=company.id)
    scope = CompanyScope(
        company_id=company.id, principal=Principal(subject="t", company_ids=frozenset({"*"}))
    )
    project = await content_service.create_scene_project(
        scope=scope, brand_id=brand.id, topic="چند صحنه", target_duration_seconds=15,
        generation_mode="multi_scene",
    )
    await db_session.commit()
    service = _service(db_session, MockSceneVideoProvider())

    jobs = await service.prepare_scenes(project, _scenario(count=3), provider_id="mock")
    assert len(jobs) == 3
    assert jobs[0].scene_key == "scene-00"
    assert "AUDIO:" in jobs[0].prompt


async def test_multi_scene_local_character_image_is_uploaded_only_once(
    db_session, content_service, company_and_platform, tmp_path
):
    company, _ = company_and_platform
    character_root = tmp_path / "Paltform_charactar" / "Vandiyar"
    character_root.mkdir(parents=True)
    (character_root / "Emerald Blazer.png").write_bytes(
        b"\x89PNG\r\n\x1a\n" + b"\x00" * 1100
    )
    brand = await content_service.ensure_vandefence_brand(company_id=company.id)
    scope = CompanyScope(
        company_id=company.id, principal=Principal(subject="t", company_ids=frozenset({"*"}))
    )
    project = await content_service.create_scene_project(
        scope=scope,
        brand_id=brand.id,
        topic="چند صحنه با شخصیت",
        target_duration_seconds=15,
        generation_mode="multi_scene",
        character_key="Vandiyar/Emerald Blazer",
    )
    await db_session.commit()
    hosted = "https://tempfile.example.test/vandad/character-references/Emerald Blazer.png"
    provider = _UploadingReferenceMock(hosted_url=hosted, poll_steps=1)
    service = _service(
        db_session, provider, characters_root=tmp_path / "Paltform_charactar"
    )

    jobs = await service.prepare_scenes(
        project, _scenario(count=3, duration=5.0), provider_id="mockupload"
    )
    await db_session.commit()
    summary = await service.submit_pending(project)
    await db_session.commit()

    assert summary["submitted"] == 3
    # A storyboard reuses one character: exactly one free upload is performed.
    assert len(provider.uploads) == 1
    assert [request.reference_image_url for request in provider.requests] == [hosted] * 3
    original_bytes = (character_root / "Emerald Blazer.png").read_bytes()
    expected_sha256 = hashlib.sha256(original_bytes).hexdigest()
    assert provider.uploads[0][0] == original_bytes
    assert project.scene_options["character_image_sha256"] == expected_sha256
    assert all(job.submission_metadata["character_image_sha256"] == expected_sha256 for job in jobs)
    assert all("REFERENCE IMAGE (immutable source)" in job.prompt for job in jobs)


async def test_prepare_scenes_rejects_duplicate_visuals_inside_one_storyboard(
    db_session, content_service, company_and_platform
):
    from app.core.exceptions import ConflictError
    company, _ = company_and_platform
    brand = await content_service.ensure_vandefence_brand(company_id=company.id)
    scope = CompanyScope(company_id=company.id, principal=Principal(subject="t", company_ids=frozenset({"*"})))
    project = await content_service.create_scene_project(
        scope=scope, brand_id=brand.id, topic="تست جلوگیری از صحنه تکراری",
        target_duration_seconds=12, generation_mode="multi_scene",
    )
    await db_session.commit()
    scenario = _scenario(count=2, duration=6.0)
    scenes = [scenario.scenes[0], scenario.scenes[1].model_copy(update={"visual_prompt": scenario.scenes[0].visual_prompt})]
    scenario = scenario.model_copy(update={"scenes": scenes})
    service = _service(db_session, MockSceneVideoProvider())
    with pytest.raises(ConflictError, match="repeats a visual"):
        await service.prepare_scenes(project, scenario, provider_id="mock")


async def test_single_shot_never_silently_switches_to_multi_scene_jobs(
    db_session, content_service, company_and_platform
):
    company, _ = company_and_platform
    project = await _single_shot_project(
        db_session, content_service, company.id, character_key="Vandiyar/Emerald Blazer"
    )
    service = _service(db_session, _ReferenceImageMock())
    await service.prepare_scenes(project, _scenario(count=3, duration=5.0), provider_id="mockref")
    await db_session.commit()

    # A prepared single-shot project must not silently gain per-scene jobs.
    project.scene_options = {**(project.scene_options or {}), "generation_mode": "multi_scene"}
    with pytest.raises(ConflictError):
        await service.prepare_scenes(project, _scenario(count=3), provider_id="mockref")

    project.scene_options = {**(project.scene_options or {}), "generation_mode": "single_shot"}
    jobs = await service.prepare_scenes(
        project, _scenario(count=3, duration=5.0), provider_id="mockref"
    )
    assert len(jobs) == 1
    assert len(await _jobs(db_session, project.id)) == 1
