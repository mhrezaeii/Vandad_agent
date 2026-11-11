"""Scene-video orchestration: prepare, submit, poll, retrieve, cancel.

The service is provider-neutral: it resolves a ``SceneVideoProvider`` from the
scene-video registry and persists every provider job on ``SceneVideoJob``.
That persisted job id is the idempotency gate — a worker restart, a retried
node, or a duplicate claim can never submit (or bill) the same scene twice.

Quota accounting is local and per company/provider/day. The counter is the
platform's own guard; it never claims to know a vendor's remaining credits
beyond what the provider or operator configured.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError
from app.integrations.video import (
    SceneJobState,
    SceneVideoPermanentError,
    SceneVideoProvider,
    SceneVideoProviderRegistry,
    SceneVideoQuotaExhaustedError,
    SceneVideoRequest,
    SceneVideoTransientError,
)
from app.models import (
    ArtifactKind,
    BrandCharacter,
    ContentProject,
    ProjectStatus,
    ProviderQuotaUsage,
    ProviderUsageRecord,
    SceneVideoJob,
    SceneVideoJobStatus,
)
from app.schemas.scenario import Scenario
from app.services.artifact_service import ArtifactService
from app.services.audit_service import AuditService
from app.services.provider_budget_service import ProviderBudgetService, parse_reported_cost
from app.storage.base import ArtifactStorage

_SCENE_RESOURCE = "scene_video_job"
_PREVIEW_BYTES = 64 * 1024


def build_scene_provider_prompt(scene: Any, scenario: Scenario) -> str:
    """Translate structured storyboard direction into a provider-ready prompt."""
    lines = [
        f"SCENE TYPE: {scene.scene_type}",
        f"SPEAKER: {scene.speaker}",
        f"VISUAL STYLE: {scenario.visual_style or 'consistent premium brand film'}",
    ]
    if scene.scene_type == "presenter":
        lines.append(
            "PRESENTER IDENTITY: Keep the recurring brand character visually identical "
            "to every other presenter scene."
        )
        if scenario.character_name:
            lines.append(f"CHARACTER NAME: {scenario.character_name}")
        if scenario.character_description:
            lines.append(f"CHARACTER REFERENCE DESCRIPTION: {scenario.character_description}")
        if scene.dialogue:
            lines.append(f"SPOKEN DIALOGUE (Persian; do not render as on-screen text): {scene.dialogue}")
        lines.append("Frame as a polished medium close-up with natural expression and direct eye contact.")
    elif scene.scene_type == "product_demo":
        lines.append(
            "PRODUCT DEMO: Show a believable close-up or over-the-shoulder interaction with the product UI. "
            "Do not invent readable labels, metrics, customer data or product features."
        )
    elif scene.scene_type == "motion_graphic":
        lines.append(
            "MOTION GRAPHICS: Use clean brand-colored shapes, iconography and kinetic "
            "composition; no generated text."
        )
    if scene.camera_direction:
        lines.append(f"CAMERA: {scene.camera_direction}")
    lines.append(f"TRANSITION INTO NEXT SCENE: {scene.transition}")
    lines.append(f"VISUAL PROMPT: {scene.visual_prompt}")
    return "\n".join(lines)


class SceneVideoService:
    def __init__(
        self,
        session: AsyncSession,
        registry: SceneVideoProviderRegistry,
        *,
        storage: ArtifactStorage | None = None,
        daily_quota: int | None = None,
        max_attempts: int = 3,
        max_size_bytes: int = 2 * 1024**3,
    ) -> None:
        self.session = session
        self.registry = registry
        self.storage = storage
        self.daily_quota = daily_quota
        self.max_attempts = max(1, int(max_attempts))
        self.max_size_bytes = max_size_bytes
        self.audit = AuditService(session)

    async def _character_reference_image(self, project: ContentProject) -> str | None:
        """Return the tenant-owned source portrait for this brand, if configured."""
        result = await self.session.execute(
            select(BrandCharacter.source_image_url)
            .where(
                BrandCharacter.brand_id == project.brand_id,
                BrandCharacter.company_id == project.company_id,
                BrandCharacter.source_image_url.is_not(None),
            )
            .limit(1)
        )
        return result.scalar_one_or_none()

    # -------------------------------------------------------------- preparation
    async def prepare_scenes(
        self, project: ContentProject, scenario: Scenario, *, provider_id: str
    ) -> list[SceneVideoJob]:
        """Create one job per scene; idempotent on ``(project, scene_index)``."""
        provider = self.registry.get(provider_id)
        presenter_scenes = [
            scene
            for scene in scenario.scenes
            if scene.scene_type == "presenter" and scene.speaker == "brand_character"
        ]
        unsupported_presenters = [
            scene.index for scene in presenter_scenes if scene.dialogue and not provider.supports_lipsync
        ]
        if unsupported_presenters:
            raise ConflictError(
                "Spoken brand-character presenter scenes require real image/audio lip-sync support; "
                f"provider {provider.provider_id!r} cannot generate them (scene indexes: "
                f"{unsupported_presenters})."
            )
        if presenter_scenes and provider.supports_reference_image:
            if not await self._character_reference_image(project):
                raise ConflictError(
                    "A saved public source_image_url is required for brand-character image-to-video scenes."
                )
        elif presenter_scenes and not provider.supports_lipsync:
            raise ConflictError(
                "Brand-character presenter scenes require a provider with real image/audio lip-sync support "
                "or image-reference support; "
                f"provider {provider.provider_id!r} cannot generate them."
            )
        existing = {
            job.scene_index: job
            for job in (
                await self.session.execute(
                    select(SceneVideoJob).where(SceneVideoJob.project_id == project.id)
                )
            ).scalars()
        }

        jobs: list[SceneVideoJob] = []
        for scene in scenario.scenes:
            job = existing.get(scene.index)
            if job is None:
                job = SceneVideoJob(
                    company_id=project.company_id,
                    project_id=project.id,
                    scene_index=scene.index,
                    scene_key=f"scene-{scene.index:02d}",
                    provider=provider.provider_id,
                    status=SceneVideoJobStatus.PENDING.value,
                    prompt=build_scene_provider_prompt(scene, scenario),
                    narration=scene.narration,
                    on_screen_text=scene.on_screen_text,
                    subtitle_text=scene.subtitle_text or scene.narration,
                    duration_seconds=scene.duration_seconds,
                    max_attempts=self.max_attempts,
                )
                self.session.add(job)
                await self.session.flush()
                self.audit.record(
                    event_type="scene.prepared",
                    action="create",
                    company_id=project.company_id,
                    resource_type=_SCENE_RESOURCE,
                    resource_id=job.id,
                    metadata={
                        "project_id": str(project.id),
                        "scene_index": scene.index,
                        "provider": provider.provider_id,
                    },
                )
            else:
                # Keep the persisted prompt/narration in sync with an approved
                # scenario without resetting provider state.
                job.prompt = build_scene_provider_prompt(scene, scenario)
                job.narration = scene.narration
                job.on_screen_text = scene.on_screen_text
                job.subtitle_text = scene.subtitle_text or scene.narration
                job.duration_seconds = scene.duration_seconds
                job.provider = provider.provider_id
            jobs.append(job)

        project.scenario = scenario.model_dump(mode="json")
        project.scene_video_provider = provider.provider_id
        project.status = ProjectStatus.SCENES_PREPARED.value
        await self.session.flush()
        return jobs

    # ---------------------------------------------------------------- submission
    async def submit_pending(
        self, project: ContentProject, *, execution_id: uuid.UUID | None = None
    ) -> dict[str, Any]:
        """Submit every not-yet-submitted scene exactly once.

        Jobs that already carry a ``provider_job_id`` are skipped. Transient
        provider failures are re-raised after persisting the attempt so the
        workflow engine can retry the node with backoff.
        """
        provider = self._provider_for(project)
        jobs = await self._jobs(project)
        if not jobs:
            raise ConflictError("No scenes have been prepared for this project.")

        submitted = 0
        first_transient: Exception | None = None
        first_permanent: SceneVideoPermanentError | None = None
        scenario_scenes = (project.scenario or {}).get("scenes", [])
        presenter_indexes = {
            int(scene.get("index", -1))
            for scene in scenario_scenes
            if scene.get("scene_type") == "presenter"
            and scene.get("speaker") == "brand_character"
        }
        character_reference_image = (
            await self._character_reference_image(project)
            if presenter_indexes and provider.supports_reference_image
            else None
        )
        for job in jobs:
            if job.provider_job_id is not None:
                continue
            if job.status in (
                SceneVideoJobStatus.CANCELLED.value,
                SceneVideoJobStatus.COMPLETED.value,
                SceneVideoJobStatus.DOWNLOADED.value,
            ):
                continue
            if job.attempts >= job.max_attempts:
                job.status = SceneVideoJobStatus.FAILED.value
                job.error_code = job.error_code or "max_attempts_exceeded"
                job.error_message = job.error_message or "The scene exceeded its retry budget."
                continue

            await self._reserve_quota(project.company_id, provider)
            request = SceneVideoRequest(
                scene_id=f"{project.id}:{job.scene_index}",
                prompt=job.prompt,
                duration_seconds=job.duration_seconds,
                reference_image_url=(
                    character_reference_image
                    if job.scene_index in presenter_indexes
                    else None
                ),
                idempotency_key=f"vandad-scene-{job.id}",
                metadata={"project_id": str(project.id), "scene_index": job.scene_index},
            )
            job.attempts += 1
            try:
                provider_job_id = await provider.generate_video(request)
            except SceneVideoQuotaExhaustedError as exc:
                job.status = SceneVideoJobStatus.FAILED.value
                job.error_code = exc.code
                job.error_message = exc.message
                await self.session.flush()
                raise
            except SceneVideoTransientError as exc:
                job.error_code = exc.code
                job.error_message = exc.message
                if job.attempts >= job.max_attempts:
                    job.status = SceneVideoJobStatus.FAILED.value
                await self.session.flush()
                first_transient = first_transient or exc
                continue
            except SceneVideoPermanentError as exc:
                job.status = SceneVideoJobStatus.FAILED.value
                job.error_code = exc.code
                job.error_message = exc.message
                await self.session.flush()
                # Scenes are independent: preserve this failure but continue
                # submitting the remaining scenes in the same storyboard.
                first_permanent = first_permanent or exc
                continue
            except Exception as exc:  # unexpected provider failure
                job.status = SceneVideoJobStatus.FAILED.value
                job.error_code = getattr(exc, "code", "provider_error")
                job.error_message = getattr(exc, "message", str(exc))
                await self.session.flush()
                raise

            job.provider_job_id = str(provider_job_id)
            job.status = SceneVideoJobStatus.SUBMITTED.value
            job.started_at = datetime.now(UTC)
            job.error_code = None
            job.error_message = None
            submitted += 1
            self.audit.record(
                event_type="scene.submitted",
                action="submit",
                company_id=project.company_id,
                resource_type=_SCENE_RESOURCE,
                resource_id=job.id,
                metadata={
                    "provider": provider.provider_id,
                    "provider_job_id": str(provider_job_id),
                    "scene_index": job.scene_index,
                },
            )

        if submitted or any(job.provider_job_id for job in jobs):
            project.status = ProjectStatus.VIDEO_GENERATING.value
        await self.session.flush()
        if first_transient is not None:
            raise first_transient
        if first_permanent is not None:
            raise first_permanent
        return {
            "submitted": submitted,
            "total": len(jobs),
            "provider": provider.provider_id,
            "execution_id": str(execution_id) if execution_id else None,
        }

    # -------------------------------------------------------------------- polling
    async def poll_jobs(self, project: ContentProject) -> dict[str, Any]:
        """Refresh every in-flight scene and roll the project status forward."""
        provider = self._provider_for(project)
        jobs = await self._jobs(project)

        for job in jobs:
            if job.provider_job_id is None:
                continue
            if job.status not in (
                SceneVideoJobStatus.SUBMITTED.value,
                SceneVideoJobStatus.GENERATING.value,
            ):
                continue
            status = await provider.get_generation_status(job.provider_job_id)
            job.provider_status = status.state
            job.progress = status.progress
            raw_usage = (status.raw or {}).get("usage")
            reported_cost = parse_reported_cost(raw_usage) if isinstance(raw_usage, dict) else None
            if reported_cost is None:
                reported_cost = parse_reported_cost(status.raw or {})
            if reported_cost is not None:
                job.cost_metadata = {
                    **(job.cost_metadata or {}),
                    "reported_cost_usd": str(reported_cost),
                    "provider": provider.provider_id,
                }
            if status.video_url:
                job.video_url = status.video_url
            if status.is_completed:
                job.status = SceneVideoJobStatus.COMPLETED.value
                job.progress = 1.0
                job.completed_at = datetime.now(UTC)
                job.error_code = None
                job.error_message = None
            elif status.state == SceneJobState.FAILED.value:
                self._handle_scene_failure(job, status.error_code, status.error_message)
            elif status.state == SceneJobState.CANCELLED.value:
                job.status = SceneVideoJobStatus.CANCELLED.value
            else:
                job.status = SceneVideoJobStatus.GENERATING.value

        summary = self._summarize(jobs)
        if summary["all_terminal"]:
            if summary["failed"]:
                project.status = ProjectStatus.FAILED.value
                project.error_message = "One or more scene videos failed to generate."
            elif summary["cancelled"]:
                project.status = ProjectStatus.CANCELLED.value
            else:
                project.status = ProjectStatus.VIDEO_GENERATED.value
                await self._settle_project_video_budget(project, jobs)
        else:
            project.status = ProjectStatus.VIDEO_GENERATING.value
        await self.session.flush()
        return summary

    async def _settle_project_video_budget(
        self, project: ContentProject, jobs: list[SceneVideoJob]
    ) -> None:
        """Settle a paid scene reservation only when every clip reports USD cost."""
        metadata = project.cost_metadata or {}
        reservation_id = metadata.get("budget_reservation_id")
        if not reservation_id or not jobs:
            return
        try:
            parsed_id = uuid.UUID(str(reservation_id))
        except (TypeError, ValueError):
            return
        reservation = await self.session.scalar(
            select(ProviderUsageRecord).where(
                ProviderUsageRecord.id == parsed_id,
                ProviderUsageRecord.company_id == project.company_id,
            ).with_for_update()
        )
        if reservation is None or reservation.status == "settled":
            return
        costs: list[Decimal] = []
        for job in jobs:
            value = (job.cost_metadata or {}).get("reported_cost_usd")
            if value is None:
                return
            try:
                amount = Decimal(str(value))
            except (InvalidOperation, ValueError):
                return
            if not amount.is_finite() or amount < 0:
                return
            costs.append(amount)
        await ProviderBudgetService(self.session).settle(
            reservation,
            usage={
                "cost_usd": str(sum(costs, Decimal("0"))),
                "currency": "USD",
                "model": str(metadata.get("provider_id") or reservation.provider_id),
            },
        )
        project.cost_metadata = {
            **metadata,
            "status": "settled",
            "actual_cost_usd": str(sum(costs, Decimal("0"))),
        }

    # ---------------------------------------------------------------- retrieval
    async def retrieve_completed(
        self, project: ContentProject, *, execution_id: uuid.UUID
    ) -> dict[str, Any]:
        """Download every completed scene into ArtifactStorage exactly once."""
        if self.storage is None:
            raise ConflictError("Artifact storage is not configured.")
        provider = self._provider_for(project)
        artifact_service = ArtifactService(
            self.session, self.storage, max_size_bytes=self.max_size_bytes
        )
        jobs = await self._jobs(project)
        downloaded = 0
        for job in jobs:
            if job.status != SceneVideoJobStatus.COMPLETED.value or job.artifact_id is not None:
                continue
            if not job.provider_job_id:
                continue
            prefix = bytearray()

            async def _chunks(job=job, prefix=prefix):  # noqa: ANN001 - closure
                async for chunk in provider.retrieve_video(job.provider_job_id):
                    if len(prefix) < _PREVIEW_BYTES:
                        prefix.extend(chunk[: _PREVIEW_BYTES - len(prefix)])
                    yield chunk

            artifact = await artifact_service.store_artifact(
                execution_id,
                company_id=project.company_id,
                kind=ArtifactKind.VIDEO.value,
                content_type="video/mp4",
                chunks=_chunks(),
                filename=f"{job.scene_key}-{job.provider_job_id}.mp4",
            )
            validation = await provider.validate_output(bytes(prefix), content_type="video/mp4")
            if not validation.ok:
                await artifact_service.delete_artifact(
                    artifact.id, company_id=project.company_id
                )
                job.status = SceneVideoJobStatus.FAILED.value
                job.error_code = "invalid_video"
                job.error_message = "; ".join(validation.issues) or "The downloaded clip is invalid."
                await self.session.flush()
                continue

            job.artifact_id = artifact.id
            job.status = SceneVideoJobStatus.DOWNLOADED.value
            job.cost_metadata = {
                **(job.cost_metadata or {}),
                "provider": provider.provider_id,
                "provider_job_id": job.provider_job_id,
                "size_bytes": artifact.size_bytes,
                "sha256": artifact.sha256,
            }
            downloaded += 1
            self.audit.record(
                event_type="scene.downloaded",
                action="download",
                company_id=project.company_id,
                resource_type=_SCENE_RESOURCE,
                resource_id=job.id,
                metadata={
                    "artifact_id": str(artifact.id),
                    "size_bytes": artifact.size_bytes,
                    "sha256": artifact.sha256,
                },
            )

        await self.session.flush()
        summary = self._summarize(jobs)
        if summary["failed"]:
            project.status = ProjectStatus.FAILED.value
            project.error_message = "One or more scene videos could not be downloaded."
        return {"downloaded": downloaded, **summary}

    # ------------------------------------------------------------------- cancel
    async def cancel(self, project: ContentProject) -> dict[str, Any]:
        provider = self._provider_for(project)
        jobs = await self._jobs(project)
        cancelled = 0
        for job in jobs:
            if job.status in (
                SceneVideoJobStatus.COMPLETED.value,
                SceneVideoJobStatus.DOWNLOADED.value,
                SceneVideoJobStatus.FAILED.value,
                SceneVideoJobStatus.CANCELLED.value,
            ):
                continue
            if job.provider_job_id and provider.supports_cancellation:
                await provider.cancel_generation(job.provider_job_id)
            job.status = SceneVideoJobStatus.CANCELLED.value
            cancelled += 1
        project.status = ProjectStatus.CANCELLED.value
        await self.session.flush()
        return {"cancelled": cancelled, "total": len(jobs)}

    # ------------------------------------------------------------------ helpers
    def _provider_for(self, project: ContentProject) -> SceneVideoProvider:
        provider_id = project.scene_video_provider
        if not provider_id:
            raise ConflictError("The project has no scene-video provider configured.")
        return self.registry.get(provider_id)

    async def _jobs(self, project: ContentProject) -> list[SceneVideoJob]:
        return list(
            (
                await self.session.execute(
                    select(SceneVideoJob)
                    .where(SceneVideoJob.project_id == project.id)
                    .order_by(SceneVideoJob.scene_index)
                )
            ).scalars()
        )

    async def list_jobs(self, project: ContentProject) -> list[SceneVideoJob]:
        """Public, ordered scene list for orchestration and API reads."""
        return await self._jobs(project)

    def _handle_scene_failure(
        self, job: SceneVideoJob, code: str | None, message: str | None
    ) -> None:
        job.error_code = code or "provider_failed"
        job.error_message = message or "The provider reported a failed generation."
        if job.attempts < job.max_attempts:
            # Allow a later submit_pending call to resubmit this scene.
            job.submission_metadata = {
                **(job.submission_metadata or {}),
                "failed_provider_job_id": job.provider_job_id,
            }
            job.provider_job_id = None
            job.provider_status = None
            job.status = SceneVideoJobStatus.PENDING.value
        else:
            job.status = SceneVideoJobStatus.FAILED.value

    @staticmethod
    def _summarize(jobs: list[SceneVideoJob]) -> dict[str, Any]:
        total = len(jobs)
        completed = sum(
            1
            for job in jobs
            if job.status
            in (SceneVideoJobStatus.COMPLETED.value, SceneVideoJobStatus.DOWNLOADED.value)
        )
        failed = sum(1 for job in jobs if job.status == SceneVideoJobStatus.FAILED.value)
        cancelled = sum(1 for job in jobs if job.status == SceneVideoJobStatus.CANCELLED.value)
        pending = total - completed - failed - cancelled
        return {
            "total": total,
            "completed": completed,
            "failed": failed,
            "cancelled": cancelled,
            "pending": pending,
            "all_terminal": pending == 0,
        }

    async def _reserve_quota(
        self, company_id: uuid.UUID, provider: SceneVideoProvider
    ) -> ProviderQuotaUsage:
        limit = self.daily_quota if self.daily_quota is not None else provider.quota_limit_per_day()
        period = datetime.now(UTC).date()
        row = await self._find_quota(company_id, provider.provider_id, period)
        if row is None:
            row = ProviderQuotaUsage(
                company_id=company_id,
                provider=provider.provider_id,
                period_date=period,
                units_used=0,
                units_limit=limit,
            )
            try:
                async with self.session.begin_nested():
                    self.session.add(row)
                    await self.session.flush()
            except IntegrityError:
                row = await self._find_quota(company_id, provider.provider_id, period)
                if row is None:
                    raise
        if limit is not None and row.units_used >= limit:
            raise SceneVideoQuotaExhaustedError(
                f"The daily quota for provider '{provider.provider_id}' is exhausted.",
                code="quota_exhausted",
            )
        row.units_used += 1
        row.units_limit = limit
        await self.session.flush()
        return row

    async def _find_quota(
        self, company_id: uuid.UUID, provider_id: str, period: date
    ) -> ProviderQuotaUsage | None:
        return (
            await self.session.execute(
                select(ProviderQuotaUsage).where(
                    ProviderQuotaUsage.company_id == company_id,
                    ProviderQuotaUsage.provider == provider_id,
                    ProviderQuotaUsage.period_date == period,
                )
            )
        ).scalar_one_or_none()
