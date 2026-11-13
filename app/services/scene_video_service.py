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
from math import gcd
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError
from app.integrations.video import (
    SceneJobState,
    SceneVideoCompositionRequest,
    SceneVideoPermanentError,
    SceneVideoProvider,
    SceneVideoProviderRegistry,
    SceneVideoQualityRequest,
    SceneVideoQuotaExhaustedError,
    SceneVideoRequest,
    SceneVideoTransientError,
)
from app.models import (
    ArtifactKind,
    ContentProject,
    ProjectStatus,
    ProviderQuotaUsage,
    ProviderUsageRecord,
    SceneVideoJob,
    SceneVideoJobStatus,
)
from app.schemas.scenario import Scenario
from app.security.redaction import redact_text
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
        f"VISUAL STYLE: {scenario.visual_style or 'consistent premium brand film'}",
    ]
    if scene.scene_type == "product_demo":
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

    # -------------------------------------------------------------- preparation
    async def prepare_scenes(
        self, project: ContentProject, scenario: Scenario, *, provider_id: str
    ) -> list[SceneVideoJob]:
        """Create one job per scene; idempotent on ``(project, scene_index)``."""
        provider = self.registry.get(provider_id)
        # Scene generation and final quality review are separate capabilities.
        # A generation-only provider may prepare and submit clips; finalization
        # still fails closed unless composition and quality review are configured.
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
        pending_jobs = [
            job for job in jobs
            if job.provider_job_id is None
            and job.status not in (
                SceneVideoJobStatus.CANCELLED.value,
                SceneVideoJobStatus.COMPLETED.value,
                SceneVideoJobStatus.DOWNLOADED.value,
            )
            and job.attempts < job.max_attempts
        ]
        if pending_jobs:
            if not provider.supports_health_check:
                raise SceneVideoPermanentError(
                    "The selected provider has no live health probe; paid submission is blocked.",
                    code="provider_health_probe_unsupported",
                )
            health = await provider.check_health()
            if not health.healthy:
                raise SceneVideoTransientError(
                    "The selected provider is unhealthy; paid submission was not started.",
                    code="provider_unhealthy",
                )

        submitted = 0
        first_transient: Exception | None = None
        first_permanent: SceneVideoPermanentError | None = None
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
                if exc.code == "provider_submission_unknown":
                    # No upstream idempotency contract exists. Persist an
                    # operator-reconciliation state and exhaust automatic
                    # retries so a lost POST response cannot double-bill.
                    job.attempts = job.max_attempts
                    job.status = SceneVideoJobStatus.FAILED.value
                    job.submission_metadata = {
                        **(job.submission_metadata or {}),
                        "submission_outcome": "unknown",
                        "reconcile_before_retry": True,
                    }
                elif job.attempts >= job.max_attempts:
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
                # Scene completion is not the end of the paid operation: the
                # provider still has to compose the final deliverable. Keep the
                # task reservation open until composition has completed.
                project.status = ProjectStatus.VIDEO_GENERATED.value
        else:
            project.status = ProjectStatus.VIDEO_GENERATING.value
        await self.session.flush()
        return summary

    async def _settle_project_video_budget(
        self,
        project: ContentProject,
        jobs: list[SceneVideoJob],
        *,
        composition_cost_usd: Decimal | None,
        quality_cost_usd: Decimal | None,
    ) -> None:
        """Settle only after every scene, composition and review report USD cost."""
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
        # Missing composition usage is unknown spend, not zero spend. Keep
        # the original reservation charged until an operator reconciles it.
        if (
            composition_cost_usd is None
            or quality_cost_usd is None
            or not composition_cost_usd.is_finite()
            or not quality_cost_usd.is_finite()
            or composition_cost_usd < 0
            or quality_cost_usd < 0
        ):
            return
        costs: list[Decimal] = [composition_cost_usd, quality_cost_usd]
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

    async def finalize_project_video(
        self,
        project: ContentProject,
        *,
        execution_id: uuid.UUID,
        width: int,
        height: int,
        brand_context: dict[str, Any] | None = None,
    ) -> tuple[Any, dict[str, Any]]:
        """Compose scene clips through an AI provider and store its final output.

        Composition job state is isolated in assembly_metadata and remains
        separate from legacy project-level provider submission metadata.
        """
        if self.storage is None:
            raise ConflictError("Artifact storage is not configured.")

        # Serialize competing workers on the project row. Provider-side
        # idempotency remains the second line of defence for ambiguous retries.
        locked_project = await self.session.scalar(
            select(ContentProject)
            .where(
                ContentProject.id == project.id,
                ContentProject.company_id == project.company_id,
            )
            .with_for_update()
        )
        if locked_project is None:
            raise ConflictError("The content project is no longer available.")
        project = locked_project

        artifact_service = ArtifactService(
            self.session, self.storage, max_size_bytes=self.max_size_bytes
        )
        if project.final_artifact_id is not None:
            artifact = await artifact_service.get_artifact(
                project.final_artifact_id, company_id=project.company_id
            )
            return artifact, dict(project.assembly_metadata or {})

        provider = self._provider_for(project)
        if not provider.supports_quality_review:
            raise SceneVideoPermanentError(
                "The selected AI provider cannot review final-video quality.",
                code="provider_quality_review_unsupported",
            )
        jobs = sorted(await self._jobs(project), key=lambda item: item.scene_index)
        if not jobs or any(not job.video_url for job in jobs):
            raise ConflictError(
                "Every scene must have a provider-hosted video URL before AI composition."
            )

        assembly = dict(project.assembly_metadata or {})
        composition_job_id = assembly.get("composition_job_id")
        if not composition_job_id:
            if not provider.supports_composition:
                raise SceneVideoPermanentError(
                    "The selected AI provider cannot compose a final storyboard video.",
                    code="provider_composition_unsupported",
                )
            await self._reserve_quota(project.company_id, provider)
            request = SceneVideoCompositionRequest(
                project_id=str(project.id),
                storyboard=dict(project.scenario or {}),
                scene_video_urls=tuple(job.video_url for job in jobs if job.video_url),
                width=width,
                height=height,
                brand_context=dict(brand_context or {}),
                idempotency_key=f"vandad-compose-{project.id}",
            )
            composition_job_id = str(await provider.compose_video(request))
            assembly.update({
                "service": "ai_provider",
                "mode": "ai_composition",
                "production_path": "ai_provider_composition",
                "provider": provider.provider_id,
                "composition_job_id": composition_job_id,
                "composition_status": SceneJobState.QUEUED.value,
            })
            project.assembly_metadata = assembly
            project.status = ProjectStatus.ASSEMBLING.value
            await self.session.flush()
            raise SceneVideoTransientError(
                "AI video composition was submitted and is still processing.",
                code="ai_composition_submitted",
            )

        if assembly.get("production_path") != "ai_provider_composition":
            raise ConflictError("The project has an incompatible finalization state.")
        if assembly.get("provider") != provider.provider_id:
            raise ConflictError("The configured provider changed after composition submission.")

        status = await provider.get_generation_status(str(composition_job_id))
        assembly["composition_status"] = status.state
        project.assembly_metadata = assembly
        if status.is_terminal and not status.is_completed:
            project.status = ProjectStatus.FAILED.value
            project.error_message = status.error_message or "AI video composition failed."
            await self.session.flush()
            raise SceneVideoPermanentError(
                "The AI provider could not compose the final video.",
                code=status.error_code or "ai_composition_failed",
            )
        if not status.is_completed:
            project.status = ProjectStatus.ASSEMBLING.value
            await self.session.flush()
            raise SceneVideoTransientError(
                "AI video composition is still processing.",
                code="ai_composition_in_progress",
            )

        preview = bytearray()

        async def chunks():
            async for chunk in provider.retrieve_video(str(composition_job_id)):
                if len(preview) < _PREVIEW_BYTES:
                    preview.extend(chunk[: _PREVIEW_BYTES - len(preview)])
                yield chunk

        artifact = await artifact_service.store_artifact(
            execution_id,
            company_id=project.company_id,
            kind=ArtifactKind.VIDEO.value,
            content_type="video/mp4",
            chunks=chunks(),
            filename=f"vandad-final-{project.id}.mp4",
        )
        validation = await provider.validate_output(bytes(preview), content_type="video/mp4")
        if not validation.ok:
            await artifact_service.delete_artifact(
                artifact.id, company_id=project.company_id
            )
            project.status = ProjectStatus.FAILED.value
            project.error_message = "The AI provider returned an invalid final video."
            await self.session.flush()
            raise SceneVideoPermanentError(
                "The AI provider returned an invalid final video.",
                code="invalid_final_video",
            )

        if not status.video_url:
            await artifact_service.delete_artifact(artifact.id, company_id=project.company_id)
            project.status = ProjectStatus.FAILED.value
            project.error_message = "The AI provider did not return a reviewable final-video URL."
            await self.session.flush()
            raise SceneVideoPermanentError(
                "The AI provider did not return a reviewable final-video URL.",
                code="provider_quality_review_url_missing",
            )
        try:
            quality = await provider.review_video(
                SceneVideoQualityRequest(
                    project_id=str(project.id),
                    video_url=status.video_url,
                    storyboard=dict(project.scenario or {}),
                    brand_context=dict(brand_context or {}),
                    expected_duration_seconds=sum(float(job.duration_seconds) for job in jobs),
                    aspect_ratio=f"{width // gcd(width, height)}:{height // gcd(width, height)}",
                    idempotency_key=f"vandad-quality-{project.id}",
                )
            )
        except SceneVideoTransientError:
            await artifact_service.delete_artifact(artifact.id, company_id=project.company_id)
            project.status = ProjectStatus.ASSEMBLING.value
            await self.session.flush()
            raise
        except Exception as exc:
            await artifact_service.delete_artifact(artifact.id, company_id=project.company_id)
            project.status = ProjectStatus.FAILED.value
            project.error_message = "The AI provider quality-review operation failed."
            await self.session.flush()
            if isinstance(exc, SceneVideoPermanentError):
                raise
            raise SceneVideoPermanentError(
                "The AI provider quality-review operation failed.",
                code="provider_quality_review_failed",
            ) from exc
        if (
            not isinstance(quality.accepted, bool)
            or (
                quality.score is not None
                and (
                    isinstance(quality.score, bool)
                    or not isinstance(quality.score, (int, float))
                    or not 0 <= float(quality.score) <= 1
                )
            )
            or not isinstance(quality.summary, str)
        ):
            await artifact_service.delete_artifact(artifact.id, company_id=project.company_id)
            project.status = ProjectStatus.FAILED.value
            project.error_message = "The AI provider returned an invalid quality report."
            await self.session.flush()
            raise SceneVideoPermanentError(
                "The AI provider returned an invalid quality report.",
                code="provider_quality_report_invalid",
            )
        raw_usage = (status.raw or {}).get("usage")
        reported_cost = parse_reported_cost(raw_usage) if isinstance(raw_usage, dict) else None
        if reported_cost is None:
            reported_cost = parse_reported_cost(status.raw or {})
        quality_cost = parse_reported_cost(quality.usage or {})
        await self._settle_project_video_budget(
            project,
            jobs,
            composition_cost_usd=reported_cost,
            quality_cost_usd=quality_cost,
        )
        quality_metadata = {
            "accepted": quality.accepted,
            "score": quality.score,
            "summary": redact_text(quality.summary)[:1000],
            "issues": [redact_text(str(issue))[:300] for issue in quality.issues[:20]],
            "cost_usd": str(quality_cost) if quality_cost is not None else None,
        }
        if not quality.accepted:
            await artifact_service.delete_artifact(artifact.id, company_id=project.company_id)
            project.status = ProjectStatus.FAILED.value
            project.error_message = "The AI provider rejected final-video quality."
            project.assembly_metadata = {
                **(project.assembly_metadata or {}),
                "quality_review": quality_metadata,
            }
            await self.session.flush()
            raise SceneVideoPermanentError(
                "The AI provider rejected final-video quality.",
                code="provider_quality_rejected",
            )
        metadata = {
            **assembly,
            "service": "ai_provider",
            "mode": "ai_composition",
            "provider": provider.provider_id,
            "provider_job_id": str(composition_job_id),
            "scene_count": len(jobs),
            "width": width,
            "height": height,
            "artifact_id": str(artifact.id),
            "size_bytes": artifact.size_bytes,
            "sha256": artifact.sha256,
            "provider_reported_cost_usd": str(reported_cost) if reported_cost is not None else None,
            "quality_review": quality_metadata,
            "composition_status": SceneJobState.COMPLETED.value,
        }
        project.final_artifact_id = artifact.id
        project.assembly_metadata = metadata
        project.status = ProjectStatus.COMPLETED.value
        project.error_message = None
        self.audit.record(
            event_type="scene.ai_composed",
            action="compose",
            company_id=project.company_id,
            resource_type="content_project",
            resource_id=project.id,
            metadata={
                "artifact_id": str(artifact.id),
                "provider": provider.provider_id,
                "provider_job_id": str(composition_job_id),
                "size_bytes": artifact.size_bytes,
                "sha256": artifact.sha256,
            },
        )
        await self.session.flush()
        return artifact, metadata

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
