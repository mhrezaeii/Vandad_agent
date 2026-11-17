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

import hashlib
import shutil
import tempfile
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from math import gcd
from pathlib import Path
from typing import Any

from sqlalchemy import or_, select
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
    Company,
    ContentProject,
    ProjectStatus,
    ProviderQuotaUsage,
    ProviderUsageRecord,
    SceneVideoJob,
    SceneVideoJobStatus,
    WorkflowArtifact,
)
from app.schemas.scenario import Scenario
from app.security.redaction import redact_text
from app.services.artifact_service import ArtifactService
from app.services.audit_service import AuditService
from app.services.character_service import (
    CharacterImageError,
    load_character_image_bytes,
    resolve_character_image,
)
from app.services.prompt_composer import PromptCompositionError, compose_single_shot_prompt
from app.services.provider_budget_service import ProviderBudgetService, parse_reported_cost
from app.services.scene_novelty import (
    extract_visual_signatures,
    is_duplicate_visual,
    normalize_visual_signature,
    visual_fingerprint,
)
from app.services.video_assembly import (
    clip_has_audio,
    concat_scene_clips,
    ffmpeg_binary,
    ffprobe_binary,
    probe_video_streams,
    run_ffmpeg,
)
from app.storage.base import ArtifactStorage

_SCENE_RESOURCE = "scene_video_job"
_PREVIEW_BYTES = 64 * 1024


def build_scene_provider_prompt(scene: Any, scenario: Scenario, *, has_reference_image: bool = False) -> str:
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
    if has_reference_image:
        lines.append(
            "REFERENCE IMAGE (immutable source): use the exact attached image as the "
            "only character reference. Do not edit, retouch, restyle, redraw, replace "
            "or alter the source image; preserve the same identity, face, hair, outfit, "
            "accessories and proportions in every generated frame."
        )
    spoken_text = (scene.pronunciation_text or scene.narration).strip()
    if spoken_text:
        lines.append(
            "AUDIO: warm, professional Iranian Persian (Farsi) voice-over says "
            "exactly this text and follows its diacritics: "
            f"{spoken_text} Under the voice-over, add a soft modern corporate "
            "background track and subtle UI sounds."
        )
    return "\n".join(lines)


def _scenario_pronunciation_text(scenario: Scenario) -> str:
    parts = [
        (scene.pronunciation_text or scene.narration).strip()
        for scene in sorted(scenario.scenes, key=lambda item: item.index)
    ]
    return " ".join(part for part in parts if part)


def _scenario_visual_signatures(scenario: Scenario) -> list[str]:
    return [
        normalize_visual_signature(scene.visual_prompt)
        for scene in sorted(scenario.scenes, key=lambda item: item.index)
    ]


def _scenario_visual_fingerprints(scenario: Scenario) -> list[str]:
    return [
        visual_fingerprint(scene.visual_prompt)
        for scene in sorted(scenario.scenes, key=lambda item: item.index)
    ]


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
        screenshots_root: str | Path | None = None,
        characters_root: str | Path | None = None,
    ) -> None:
        self.session = session
        self.registry = registry
        self.storage = storage
        self.daily_quota = daily_quota
        self.max_attempts = max(1, int(max_attempts))
        self._screenshots_root = Path(screenshots_root) if screenshots_root else None
        self._characters_root = Path(characters_root) if characters_root else None
        self.max_size_bytes = max_size_bytes
        self.audit = AuditService(session)

    @staticmethod
    def _generation_mode(project: ContentProject) -> str:
        options = project.scene_options or {}
        mode = str(options.get("generation_mode") or "").strip().lower()
        return mode or "multi_scene"

    # -------------------------------------------------------------- preparation
    async def prepare_scenes(
        self, project: ContentProject, scenario: Scenario, *, provider_id: str
    ) -> list[SceneVideoJob]:
        """Create provider jobs for the approved scenario.

        ``multi_scene`` (default) creates one job per storyboard beat.
        ``single_shot`` combines the complete scenario into ONE composed
        prompt and creates exactly ONE job, so the provider receives exactly
        one generation request for the whole video.
        """
        provider = self.registry.get(provider_id)
        # Scene generation and final quality review are separate capabilities.
        # A generation-only provider may prepare and submit clips; finalization
        # still fails closed unless composition and quality review are configured.
        # Serialize preparation per tenant so parallel projects cannot both
        # pass the novelty check before creating paid generation jobs.
        await self.session.execute(
            select(Company.id).where(Company.id == project.company_id).with_for_update()
        )
        existing = {
            job.scene_index: job
            for job in (
                await self.session.execute(
                    select(SceneVideoJob).where(SceneVideoJob.project_id == project.id)
                )
            ).scalars()
        }

        await self._assert_scene_novelty(project, scenario, existing)

        if self._generation_mode(project) == "single_shot":
            jobs = await self._prepare_single_shot(project, scenario, provider, existing)
        else:
            jobs = await self._prepare_multi_scene(project, scenario, provider, existing)

        project.scenario = scenario.model_dump(mode="json")
        project.scene_video_provider = provider.provider_id
        project.status = ProjectStatus.SCENES_PREPARED.value
        await self.session.flush()
        return jobs

    @staticmethod
    def _job_visual_signatures(job: SceneVideoJob) -> list[str]:
        metadata = job.submission_metadata or {}
        values = metadata.get("visual_signatures")
        if isinstance(values, list):
            return [normalize_visual_signature(str(value)) for value in values if str(value).strip()]
        value = metadata.get("visual_signature")
        if isinstance(value, str) and value.strip():
            return [normalize_visual_signature(value)]
        return extract_visual_signatures(job.prompt)

    async def _assert_scene_novelty(
        self, project: ContentProject, scenario: Scenario, existing: dict[int, SceneVideoJob]
    ) -> None:
        """Reject repeated visuals within this storyboard and recent tenant history."""
        history_result = await self.session.execute(
            select(SceneVideoJob)
            .where(
                SceneVideoJob.company_id == project.company_id,
                SceneVideoJob.project_id != project.id,
                or_(SceneVideoJob.attempts > 0, SceneVideoJob.provider_job_id.is_not(None)),
            )
            .order_by(SceneVideoJob.created_at.desc())
            .limit(500)
        )
        historical = [signature for job in history_result.scalars() for signature in self._job_visual_signatures(job)]
        scenes = sorted(scenario.scenes, key=lambda item: item.index)
        candidate_signatures = [normalize_visual_signature(scene.visual_prompt) for scene in scenes]
        single_shot_job = next((job for job in existing.values() if job.scene_key == "single-shot"), None)
        if single_shot_job and single_shot_job.attempts > 0:
            previous = self._job_visual_signatures(single_shot_job)
            if previous and previous != candidate_signatures:
                raise ConflictError("An already-submitted single-shot video cannot be changed; create a new project.")
        seen: list[str] = []
        for scene, signature in zip(scenes, candidate_signatures, strict=True):
            if not signature:
                raise ConflictError(f"Scene {scene.index + 1} has no usable visual signature.")
            if any(is_duplicate_visual(signature, previous) for previous in seen):
                raise ConflictError(
                    f"Scene {scene.index + 1} repeats a visual already used in this storyboard; "
                    "change its visual_prompt before generation."
                )
            for job in existing.values():
                if job.scene_key == "single-shot":
                    continue
                if job.scene_index == scene.index:
                    if job.attempts > 0:
                        old = self._job_visual_signatures(job)
                        if old and old[0] != signature:
                            raise ConflictError(
                                f"Scene {scene.index + 1} was already submitted and its visual prompt "
                                "cannot change."
                            )
                    continue
                if any(is_duplicate_visual(signature, previous) for previous in self._job_visual_signatures(job)):
                    raise ConflictError(
                        f"Scene {scene.index + 1} duplicates another scene in this project; "
                        "change its visual_prompt before generation."
                    )
            if any(is_duplicate_visual(signature, previous) for previous in historical):
                raise ConflictError(
                    f"Scene {scene.index + 1} duplicates a recently generated scene for this company; "
                    "create a distinct visual direction before spending provider credits."
                )
            seen.append(signature)

    async def _prepare_multi_scene(
        self,
        project: ContentProject,
        scenario: Scenario,
        provider: SceneVideoProvider,
        existing: dict[int, SceneVideoJob],
    ) -> list[SceneVideoJob]:
        single_shot_jobs = [job for job in existing.values() if job.scene_key == "single-shot"]
        if single_shot_jobs:
            raise ConflictError(
                "This project was prepared in single-shot mode; switching to multi-scene "
                "would create additional paid generation jobs. Start a new project instead.",
            )
        jobs: list[SceneVideoJob] = []
        options = project.scene_options or {}
        has_reference_image = bool(options.get("character_key") or options.get("character_image_url"))
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
                    prompt=build_scene_provider_prompt(scene, scenario, has_reference_image=has_reference_image),
                    narration=scene.pronunciation_text or scene.narration,
                    on_screen_text=scene.on_screen_text,
                    subtitle_text=scene.subtitle_text or scene.narration,
                    duration_seconds=scene.duration_seconds,
                    max_attempts=self.max_attempts,
                    submission_metadata={
                        "visual_signature": normalize_visual_signature(scene.visual_prompt),
                        "visual_fingerprint": visual_fingerprint(scene.visual_prompt),
                    },
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
                job.prompt = build_scene_provider_prompt(scene, scenario, has_reference_image=has_reference_image)
                job.narration = scene.pronunciation_text or scene.narration
                job.submission_metadata = {
                    **(job.submission_metadata or {}),
                    "visual_signature": normalize_visual_signature(scene.visual_prompt),
                    "visual_fingerprint": visual_fingerprint(scene.visual_prompt),
                }
                job.on_screen_text = scene.on_screen_text
                job.subtitle_text = scene.subtitle_text or scene.narration
                job.duration_seconds = scene.duration_seconds
                job.provider = provider.provider_id
            jobs.append(job)
        return jobs

    async def _prepare_single_shot(
        self,
        project: ContentProject,
        scenario: Scenario,
        provider: SceneVideoProvider,
        existing: dict[int, SceneVideoJob],
    ) -> list[SceneVideoJob]:
        """Prepare exactly ONE whole-video job from the complete scenario."""
        if len(existing) > 1:
            raise ConflictError(
                "This project already has per-scene jobs; single-shot preparation "
                "must start from a fresh project.",
            )
        total_duration = scenario.total_duration_seconds
        min_duration = provider.min_duration_seconds
        max_duration = provider.max_duration_seconds
        if min_duration is not None and total_duration < min_duration:
            raise SceneVideoPermanentError(
                f"The single-shot scenario is {total_duration:g}s but {provider.display_name} "
                f"requires at least {min_duration:g}s for one continuous generation.",
                code="provider_invalid_duration",
            )
        if max_duration is not None and total_duration > max_duration:
            raise SceneVideoPermanentError(
                f"The single-shot scenario is {total_duration:g}s but {provider.display_name} "
                f"generates at most {max_duration:g}s in one request. Reduce the target "
                "duration; single-shot mode never splits the video into multiple paid jobs.",
                code="provider_invalid_duration",
            )

        options = project.scene_options or {}
        character_description = str(options.get("character_description") or "").strip() or None
        try:
            prompt = compose_single_shot_prompt(
                scenario,
                character_description=character_description,
                has_reference_image=bool(options.get("character_key") or options.get("character_image_url")),
                max_characters=provider.max_prompt_characters or 5000,
            )
        except PromptCompositionError as exc:
            raise SceneVideoPermanentError(exc.message, code=exc.code) from exc

        job = existing.get(0)
        if job is None:
            job = SceneVideoJob(
                company_id=project.company_id,
                project_id=project.id,
                scene_index=0,
                scene_key="single-shot",
                provider=provider.provider_id,
                status=SceneVideoJobStatus.PENDING.value,
                prompt=prompt,
                narration=_scenario_pronunciation_text(scenario),
                on_screen_text="\n".join(
                    scene.on_screen_text for scene in sorted(scenario.scenes, key=lambda item: item.index)
                    if scene.on_screen_text
                ) or None,
                subtitle_text=scenario.narration,
                duration_seconds=total_duration,
                max_attempts=self.max_attempts,
                submission_metadata={
                    "generation_mode": "single_shot",
                    "visual_signatures": _scenario_visual_signatures(scenario),
                    "visual_fingerprints": _scenario_visual_fingerprints(scenario),
                },
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
                    "scene_index": 0,
                    "provider": provider.provider_id,
                    "generation_mode": "single_shot",
                    "beats": len(scenario.scenes),
                },
            )
        else:
            # Keep the persisted prompt in sync with an approved scenario
            # without resetting provider submission state.
            job.prompt = prompt
            job.narration = _scenario_pronunciation_text(scenario)
            job.on_screen_text = "\n".join(
                scene.on_screen_text for scene in sorted(scenario.scenes, key=lambda item: item.index)
                if scene.on_screen_text
            ) or None
            job.subtitle_text = scenario.narration
            job.duration_seconds = total_duration
            job.provider = provider.provider_id
            job.submission_metadata = {
                **(job.submission_metadata or {}),
                "generation_mode": "single_shot",
                "visual_signatures": _scenario_visual_signatures(scenario),
                "visual_fingerprints": _scenario_visual_fingerprints(scenario),
            }
        return [job]

    async def _resolve_reference_image(
        self, project: ContentProject, provider: SceneVideoProvider
    ) -> str | None:
        """Resolve the configured character reference image to a provider-fetchable URL.

        Two sources are supported (checked in order):

        1. ``scene_options["character_image_url"]`` — an operator-supplied public
           HTTPS URL (the provider adapter validates it again before submit).
        2. ``scene_options["character_key"]`` — a validated local asset from the
           character registry; providers that expose a file-upload API receive
           the uploaded public URL, so the image bytes are actually transmitted
           to the provider instead of being dropped.
        """
        if not provider.supports_reference_image:
            return None
        options = project.scene_options or {}
        external_url = str(options.get("character_image_url") or "").strip()
        character_key = str(options.get("character_key") or "").strip()
        if external_url:
            updated_options = dict(options)
            updated_options.pop("character_image_sha256", None)
            updated_options["character_image_source"] = "external_url"
            project.scene_options = updated_options
            return external_url
        if not character_key:
            return None
        uploader = getattr(provider, "upload_reference_image", None)
        if not callable(uploader):
            raise SceneVideoPermanentError(
                f"Provider {provider.provider_id} cannot host local character images; "
                "provide a public HTTPS character_image_url instead.",
                code="provider_reference_image_unsupported",
            )
        try:
            image = resolve_character_image(self._characters_root, character_key)
        except CharacterImageError as exc:
            raise SceneVideoPermanentError(exc.message, code=exc.code) from exc
        # Upload original bytes as-is: no resize, crop, re-encode or retouch.
        image_bytes = load_character_image_bytes(image)
        digest = hashlib.sha256(image_bytes).hexdigest()
        project.scene_options = {
            **options,
            "character_image_key": image.key,
            "character_image_sha256": digest,
            "character_image_source": "immutable_local_asset",
        }
        return await uploader(image_bytes, file_name=image.file_name)

    # ---------------------------------------------------------------- submission

    # ---------------------------------------------------------------- submission
    async def submit_pending(
        self, project: ContentProject, *, execution_id: uuid.UUID | None = None
    ) -> dict[str, Any]:
        """Submit pending scenes with durable pre-submit fences for non-idempotent paid providers."""
        provider = self._provider_for(project)
        jobs = list((await self.session.execute(
            select(SceneVideoJob)
            .where(SceneVideoJob.project_id == project.id)
            .order_by(SceneVideoJob.scene_index)
            .with_for_update()
        )).scalars())
        if not jobs:
            raise ConflictError("No scenes have been prepared for this project.")
        options = project.scene_options or {}
        reference_requested = bool(options.get("character_key") or options.get("character_image_url"))
        if reference_requested and not provider.supports_reference_image:
            raise SceneVideoPermanentError(
                f"Provider {provider.provider_id} cannot consume the supplied reference image; "
                "generation is blocked instead of silently switching to text-to-video.",
                code="provider_reference_image_unsupported",
            )
        if provider.requires_reference_image and not reference_requested:
            raise SceneVideoPermanentError(
                f"Provider {provider.provider_id} requires a character reference image.",
                code="provider_reference_image_required",
            )
        if provider.provider_id == "kie" and self.storage is None:
            raise ConflictError(
                "Artifact storage must be configured before paid KIE generation.",
            )

        # Paid multi-scene providers without their own composition endpoint rely
        # on local FFmpeg assembly. Verify both binaries before spending credits;
        # ffprobe is required to preserve KIE native audio across all clips.
        if len(jobs) > 1 and not (provider.supports_composition and provider.supports_quality_review):
            ffmpeg_binary()
            if ffprobe_binary() is None:
                raise SceneVideoPermanentError(
                    "ffprobe is required for safe multi-scene video assembly and audio preservation.",
                    code="ffprobe_unavailable",
                )

        submitted = 0
        first_transient: Exception | None = None
        first_permanent: SceneVideoPermanentError | None = None
        active_submission_ids: set[uuid.UUID] = set()
        now = datetime.now(UTC)

        # KIE and WaveSpeed do not document upstream idempotency for create-task
        # requests. Fence both before their billable POSTs so worker recovery can
        # never submit a duplicate after an ambiguous acknowledgement.
        durable_submit_provider = provider.provider_id == "kie"
        if durable_submit_provider:
            for job in jobs:
                metadata = job.submission_metadata or {}
                if job.provider_job_id:
                    continue
                if metadata.get("reconcile_before_retry") is True:
                    job.status = SceneVideoJobStatus.FAILED.value
                    job.attempts = job.max_attempts
                    job.error_code = "provider_submission_unknown"
                    job.error_message = (
                        f"This scene requires {provider.display_name} task-history reconciliation before retry."
                    )
                    first_permanent = first_permanent or SceneVideoPermanentError(
                        job.error_message, code="provider_submission_unknown"
                    )
                    continue
                if metadata.get("submission_outcome") != "submitting":
                    continue
                started_raw = metadata.get("submission_started_at")
                try:
                    started_at = datetime.fromisoformat(str(started_raw))
                    if started_at.tzinfo is None:
                        started_at = started_at.replace(tzinfo=UTC)
                    age_seconds = (now - started_at).total_seconds()
                except (TypeError, ValueError):
                    age_seconds = 301.0
                if age_seconds < 300:
                    active_submission_ids.add(job.id)
                    continue
                job.status = SceneVideoJobStatus.FAILED.value
                job.attempts = job.max_attempts
                job.error_code = "provider_submission_unknown"
                job.error_message = (
                    f"A previous {provider.display_name} submit was interrupted. Check provider task history "
                    "before any manual retry."
                )
                job.submission_metadata = {
                    **metadata,
                    "submission_outcome": "unknown",
                    "reconcile_before_retry": True,
                }
                first_permanent = first_permanent or SceneVideoPermanentError(
                    job.error_message, code="provider_submission_unknown"
                )

        pending_jobs = [
            job for job in jobs
            if job.id not in active_submission_ids
            and job.provider_job_id is None
            and job.status not in (
                SceneVideoJobStatus.CANCELLED.value,
                SceneVideoJobStatus.COMPLETED.value,
                SceneVideoJobStatus.DOWNLOADED.value,
            )
            and job.attempts < job.max_attempts
        ]
        if pending_jobs and provider.provider_id == "kie":
            # Check the entire remaining storyboard before the first paid call.
            # This prevents partial spend when the account cannot cover all scenes.
            estimate_credits = getattr(provider, "estimate_credits", None)
            get_credit_balance = getattr(provider, "get_credit_balance", None)
            if callable(estimate_credits) and callable(get_credit_balance):
                required_credits = sum(
                    (estimate_credits(job.duration_seconds) for job in pending_jobs), Decimal("0")
                )
                available_credits = await get_credit_balance()
                if available_credits < required_credits:
                    raise SceneVideoQuotaExhaustedError(
                        f"KIE has {available_credits} credits but the remaining storyboard requires "
                        f"{required_credits}; no scene was submitted.",
                        code="provider_insufficient_project_credits",
                    )

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

        reference_image_url: str | None = None
        if pending_jobs and provider.supports_reference_image:
            # The reference image upload is free and one project uses one
            # character, so resolve and host it once per submit instead of
            # re-uploading the same bytes for every scene in a storyboard.
            # Image-source failures must never consume the submission fence or
            # reach a billable POST.
            try:
                reference_image_url = await self._resolve_reference_image(project, provider)
                if reference_requested and not reference_image_url:
                    raise SceneVideoPermanentError(
                        "The requested reference image could not be resolved; no generation was submitted.",
                        code="provider_reference_image_missing",
                    )
            except SceneVideoPermanentError as exc:
                for job in pending_jobs:
                    job.status = SceneVideoJobStatus.FAILED.value
                    job.error_code = exc.code
                    job.error_message = exc.message
                await self.session.flush()
                raise

        for job in jobs:
            if job.provider_job_id is not None or job.id in active_submission_ids:
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
            reference_image_sha256 = (project.scene_options or {}).get("character_image_sha256")
            if reference_image_sha256:
                job.submission_metadata = {
                    **(job.submission_metadata or {}),
                    "character_image_sha256": reference_image_sha256,
                }
            request = SceneVideoRequest(
                scene_id=f"{project.id}:{job.scene_index}",
                prompt=job.prompt,
                duration_seconds=job.duration_seconds,
                reference_image_url=reference_image_url,
                idempotency_key=f"vandad-scene-{job.id}",
                metadata={
                    "project_id": str(project.id),
                    "scene_index": job.scene_index,
                    "generation_mode": self._generation_mode(project),
                    "character_image_sha256": reference_image_sha256,
                },
            )
            job.attempts += 1
            estimate = getattr(provider, "estimate_cost_usd", None)
            if callable(estimate):
                job.cost_metadata = {
                    **(job.cost_metadata or {}),
                    "provider": provider.provider_id,
                    "model": getattr(provider, "model_id", None),
                    "estimated_cost_usd": estimate(job.duration_seconds),
                    "currency": "USD",
                }
            if durable_submit_provider:
                job.submission_metadata = {
                    **(job.submission_metadata or {}),
                    "submission_outcome": "submitting",
                    "submission_started_at": datetime.now(UTC).isoformat(),
                    "reconcile_before_retry": False,
                }
                await self._record_project_video_usage(project, jobs)
                await self.session.flush()
                # Commit the submission fence and budget telemetry before any
                # billable network call. A crash can now be reconciled safely.
                await self.session.commit()
                await self.session.refresh(job)
                await self.session.refresh(project)

            try:
                provider_job_id = await provider.generate_video(request)
            except SceneVideoQuotaExhaustedError as exc:
                job.status = SceneVideoJobStatus.FAILED.value
                job.error_code = exc.code
                job.error_message = exc.message
                if durable_submit_provider:
                    job.submission_metadata = {
                        **(job.submission_metadata or {}),
                        "submission_outcome": "rejected",
                    }
                await self._record_project_video_usage(project, jobs)
                await self.session.flush()
                raise
            except SceneVideoTransientError as exc:
                job.error_code = exc.code
                job.error_message = exc.message
                if exc.code == "provider_submission_unknown":
                    job.attempts = job.max_attempts
                    job.status = SceneVideoJobStatus.FAILED.value
                    job.submission_metadata = {
                        **(job.submission_metadata or {}),
                        "submission_outcome": "unknown",
                        "reconcile_before_retry": True,
                    }
                elif durable_submit_provider and exc.code in {
                    "provider_rate_limited", "provider_preflight_transient"
                }:
                    job.status = SceneVideoJobStatus.PENDING.value
                    job.submission_metadata = {
                        **(job.submission_metadata or {}),
                        "submission_outcome": (
                            "preflight_failed" if exc.code == "provider_preflight_transient" else "rejected"
                        ),
                    }
                    job.submission_metadata.pop("submission_started_at", None)
                elif job.attempts >= job.max_attempts:
                    job.status = SceneVideoJobStatus.FAILED.value
                await self.session.flush()
                first_transient = first_transient or exc
                continue
            except SceneVideoPermanentError as exc:
                job.status = SceneVideoJobStatus.FAILED.value
                job.error_code = exc.code
                job.error_message = exc.message
                if durable_submit_provider:
                    job.submission_metadata = {
                        **(job.submission_metadata or {}),
                        "submission_outcome": "rejected",
                    }
                await self.session.flush()
                first_permanent = first_permanent or exc
                continue
            except Exception as exc:
                job.status = SceneVideoJobStatus.FAILED.value
                job.error_code = getattr(exc, "code", "provider_error")
                job.error_message = getattr(exc, "message", str(exc))
                if durable_submit_provider:
                    job.attempts = job.max_attempts
                    job.submission_metadata = {
                        **(job.submission_metadata or {}),
                        "submission_outcome": "unknown",
                        "reconcile_before_retry": True,
                    }
                await self._record_project_video_usage(project, jobs)
                await self.session.flush()
                raise

            job.provider_job_id = str(provider_job_id)
            job.status = SceneVideoJobStatus.SUBMITTED.value
            job.started_at = datetime.now(UTC)
            job.error_code = None
            job.error_message = None
            job.submission_metadata = {
                **(job.submission_metadata or {}),
                "submission_outcome": "submitted",
                **({"prediction_id": str(provider_job_id).split(":", 1)[1]}
                   if str(provider_job_id).startswith("kie:") else {}),
            }
            job.submission_metadata.pop("submission_started_at", None)
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
        await self._record_project_video_usage(project, jobs)
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
                    "actual_cost_usd": str(reported_cost),
                    "provider": provider.provider_id,
                    **({"credits_consumed": str(status.raw["credits_consumed"])}
                       if (status.raw or {}).get("credits_consumed") is not None else {}),
                }
            if (status.raw or {}).get("estimated_cost_usd") is not None:
                job.cost_metadata = {
                    **(job.cost_metadata or {}),
                    "estimated_cost_usd": str(status.raw["estimated_cost_usd"]),
                    "currency": "USD",
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
        await self._record_project_video_usage(project, jobs)
        await self.session.flush()
        return summary

    async def _record_project_video_usage(
        self, project: ContentProject, jobs: list[SceneVideoJob]
    ) -> None:
        """Persist prediction IDs, states and estimates against the project reservation."""
        metadata = project.cost_metadata or {}
        reservation_id = metadata.get("budget_reservation_id")
        if not reservation_id:
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

        predictions: list[dict[str, Any]] = []
        estimated_total = Decimal("0")
        reported_total = Decimal("0")
        for job in jobs:
            cost = job.cost_metadata or {}
            estimated = cost.get("estimated_cost_usd")
            reported = cost.get("reported_cost_usd")
            if estimated is not None:
                try:
                    amount = Decimal(str(estimated))
                    if amount.is_finite() and amount >= 0:
                        estimated_total += amount
                except (InvalidOperation, ValueError):
                    pass
            if reported is not None:
                try:
                    amount = Decimal(str(reported))
                    if amount.is_finite() and amount >= 0:
                        reported_total += amount
                except (InvalidOperation, ValueError):
                    pass
            prediction_id = (job.submission_metadata or {}).get("prediction_id")
            if prediction_id is None and str(job.provider_job_id or "").startswith("kie:"):
                prediction_id = str(job.provider_job_id).split(":", 1)[1]
            predictions.append({
                "scene_index": job.scene_index,
                "prediction_id": str(prediction_id) if prediction_id is not None else None,
                "provider_job_id": str(job.provider_job_id) if job.provider_job_id else None,
                "model_id": cost.get("model"),
                "status": job.provider_status or job.status,
                "estimated_cost_usd": str(estimated) if estimated is not None else None,
                "reported_cost_usd": str(reported) if reported is not None else None,
                "credits_consumed": cost.get("credits_consumed"),
                "submission_outcome": (job.submission_metadata or {}).get("submission_outcome"),
            })

        reservation.usage_payload = {
            **(reservation.usage_payload or {}),
            "model": str(metadata.get("provider_id") or reservation.provider_id),
            "scene_predictions": predictions,
            "estimated_scene_cost_usd": str(estimated_total),
            "reported_scene_cost_usd": str(reported_total),
            "currency": "USD",
        }
        project.cost_metadata = {
            **metadata,
            "estimated_scene_cost_usd": str(estimated_total),
            "reported_scene_cost_usd": str(reported_total),
        }

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
                "estimated_scene_cost_usd": str(sum(
                    (Decimal(str((job.cost_metadata or {}).get("estimated_cost_usd", "0"))) for job in jobs),
                    Decimal("0"),
                )),
                "reported_scene_cost_usd": str(sum(
                    (Decimal(str((job.cost_metadata or {}).get("reported_cost_usd", "0"))) for job in jobs),
                    Decimal("0"),
                )),
                "scene_predictions": [
                    {
                        "scene_index": job.scene_index,
                        "prediction_id": (job.submission_metadata or {}).get("prediction_id"),
                        "provider_job_id": job.provider_job_id,
                        "model_id": (job.cost_metadata or {}).get("model"),
                        "status": job.provider_status or job.status,
                        "estimated_cost_usd": (job.cost_metadata or {}).get("estimated_cost_usd"),
                        "reported_cost_usd": (job.cost_metadata or {}).get("reported_cost_usd"),
                        "credits_consumed": (job.cost_metadata or {}).get("credits_consumed"),
                    }
                    for job in jobs
                ],
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

        await self._record_project_video_usage(project, jobs)
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
        jobs = sorted(await self._jobs(project), key=lambda item: item.scene_index)
        if not jobs:
            raise ConflictError("No scenes have been prepared for this project.")

        if len(jobs) == 1:
            # Single-clip passthrough: the provider's one validated clip is the
            # complete deliverable. There is nothing to compose, so provider
            # composition and quality-review capabilities are not required.
            job = jobs[0]
            if job.artifact_id is None:
                raise ConflictError(
                    "The single scene must be downloaded before it can be finalized."
                )
            artifact = await artifact_service.get_artifact(
                job.artifact_id, company_id=project.company_id
            )
            output_validation: dict[str, Any] | None = None
            if provider.provider_id == "kie":
                # The paid KIE MP4 carries provider-native Persian audio that is
                # the final deliverable. Verify the stored media (audio stream,
                # duration, dimensions, codec) before marking the project complete.
                output_validation = self._verify_kie_delivery(job, artifact)
            metadata_extra = {"mode": "single_scene_passthrough", "narration": "provider_native_audio"}
            # Passthrough mode bills no composition or quality-review call, so
            # the project reservation settles from the scene's reported cost.
            await self._settle_project_video_budget(
                project,
                jobs,
                composition_cost_usd=Decimal("0"),
                quality_cost_usd=Decimal("0"),
            )
            metadata = {
                "service": "provider",
                **metadata_extra,
                "production_path": "provider_single_scene",
                "provider": provider.provider_id,
                "scene_job_id": str(job.id),
                "scene_artifact_id": str(artifact.id),
                **({"output_validation": output_validation} if output_validation else {}),
                "quality_review": {
                    "status": "not_applicable",
                    "reason": "single_provider_clip_is_final_deliverable",
                },
            }
            project.final_artifact_id = artifact.id
            project.assembly_metadata = metadata
            project.status = ProjectStatus.COMPLETED.value
            project.error_message = None
            await self.session.flush()
            return artifact, metadata

        if not (provider.supports_composition and provider.supports_quality_review):
            # The provider generated the validated scene clips but cannot
            # compose the final storyboard video itself: assemble locally.
            return await self._finalize_local_assembly(
                project, jobs, execution_id=execution_id, width=width, height=height,
            )
        if any(not job.video_url for job in jobs):
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

    async def _finalize_local_assembly(
        self,
        project: ContentProject,
        jobs: list[SceneVideoJob],
        *,
        execution_id: uuid.UUID,
        width: int,
        height: int,
    ) -> tuple[WorkflowArtifact, dict[str, Any]]:
        """Concatenate the validated scene clips locally with FFmpeg.

        Used when the provider generated the scene clips but cannot compose
        the final storyboard video itself (e.g. WaveSpeed text-to-video).
        Deterministic media stitching only — no creative generation.
        """
        artifact_service = ArtifactService(
            self.session, self.storage, max_size_bytes=self.max_size_bytes
        )
        clip_paths: list[Path] = []
        for job in sorted(jobs, key=lambda item: item.scene_index):
            if job.artifact_id is None:
                raise SceneVideoPermanentError(
                    "Every scene must be downloaded before local assembly.",
                    code="local_assembly_missing_clips",
                )
            artifact = await artifact_service.get_artifact(
                job.artifact_id, company_id=project.company_id
            )
            path = self.storage.local_path(artifact.storage_key) if self.storage else None
            if path is None:
                raise SceneVideoPermanentError(
                    "Scene clips are not available on local storage; local assembly is unavailable.",
                    code="local_assembly_storage_unavailable",
                )
            clip_paths.append(path)

        provider = self._provider_for(project)
        if provider.supports_native_audio and not all(clip_has_audio(path) for path in clip_paths):
            raise SceneVideoPermanentError(
                "A provider-native audio track is missing from one or more scene clips.",
                code="native_audio_missing",
            )

        overlays, subtitle_path = self._plan_real_product_overlays(
            project, jobs, output_dir_hint=project.id, width=width, height=height,
        )
        output_dir = Path(tempfile.mkdtemp(prefix="vandad-assembly-"))
        try:
            if subtitle_path is not None:
                moved = output_dir / subtitle_path.name
                shutil.move(str(subtitle_path), str(moved))
                subtitle_path = moved
            for index, (image, _start, _end) in enumerate(list(overlays)):
                moved = output_dir / f"overlay-{index}{image.suffix.lower()}"
                shutil.copy(str(image), str(moved))
                overlays[index] = (moved, _start, _end)
            output_path = output_dir / f"vandad-final-{project.id}.mp4"
            await concat_scene_clips(
                tuple(clip_paths), output_path=output_path, width=width, height=height,
                overlays=tuple(overlays), subtitle_path=subtitle_path,
            )
            preview = bytearray()

            async def chunks():
                with output_path.open("rb") as handle:
                    while True:
                        block = handle.read(1024 * 1024)
                        if not block:
                            break
                        if len(preview) < _PREVIEW_BYTES:
                            preview.extend(block[: _PREVIEW_BYTES - len(preview)])
                        yield block

            artifact = await artifact_service.store_artifact(
                execution_id,
                company_id=project.company_id,
                kind=ArtifactKind.VIDEO.value,
                content_type="video/mp4",
                chunks=chunks(),
                filename=output_path.name,
            )
            provider = self._provider_for(project)
            validation = await provider.validate_output(bytes(preview), content_type="video/mp4")
        finally:
            shutil.rmtree(output_dir, ignore_errors=True)

        if not validation.ok:
            await artifact_service.delete_artifact(artifact.id, company_id=project.company_id)
            project.status = ProjectStatus.FAILED.value
            project.error_message = "The assembled final video failed validation."
            await self.session.flush()
            raise SceneVideoPermanentError(
                "The assembled final video failed validation.",
                code="invalid_final_video",
            )

        # Local assembly bills no composition or quality-review call, so the
        # project reservation settles from the scenes' reported cost.
        await self._settle_project_video_budget(
            project,
            jobs,
            composition_cost_usd=Decimal("0"),
            quality_cost_usd=Decimal("0"),
        )
        metadata = {
            "service": "local_ffmpeg",
            "mode": "local_composition",
            "production_path": "local_ffmpeg_concat",
            "provider": provider.provider_id,
            "scene_count": len(jobs),
            "width": width,
            "height": height,
            "artifact_id": str(artifact.id),
            "size_bytes": artifact.size_bytes,
            "sha256": artifact.sha256,
            "scene_artifact_ids": [str(job.artifact_id) for job in jobs],
            "quality_review": {
                "status": "not_applicable",
                "reason": "local_deterministic_assembly",
            },
        }
        project.final_artifact_id = artifact.id
        project.assembly_metadata = metadata
        project.status = ProjectStatus.COMPLETED.value
        project.error_message = None
        self.audit.record(
            event_type="scene.locally_assembled",
            action="assemble",
            company_id=project.company_id,
            resource_type="content_project",
            resource_id=project.id,
            metadata={
                "artifact_id": str(artifact.id),
                "scene_count": len(jobs),
                "size_bytes": artifact.size_bytes,
                "sha256": artifact.sha256,
            },
        )
        await self.session.flush()
        return artifact, metadata

    def _verify_kie_delivery(self, job: SceneVideoJob, artifact: Any) -> dict[str, Any]:
        """Fail closed unless the paid KIE MP4 contains the expected native media."""
        path = self.storage.local_path(artifact.storage_key) if self.storage else None
        if path is None:
            raise SceneVideoPermanentError(
                "The KIE deliverable is not available on local storage for validation.",
                code="local_assembly_storage_unavailable",
            )
        probe = probe_video_streams(path)
        if probe is None:
            raise SceneVideoPermanentError(
                "ffprobe is required to verify the KIE native-audio deliverable but is not installed.",
                code="ffprobe_unavailable",
            )
        if not probe["has_video"] or not probe["video_codec"]:
            raise SceneVideoPermanentError(
                "The KIE deliverable does not contain a usable video stream.",
                code="invalid_video",
            )
        if not probe["has_audio"]:
            raise SceneVideoPermanentError(
                "The KIE deliverable is missing the expected native Persian audio track.",
                code="native_audio_missing",
            )
        if not probe["width"] or not probe["height"]:
            raise SceneVideoPermanentError(
                "The KIE deliverable reported invalid video dimensions.",
                code="invalid_video",
            )
        duration = probe["duration_seconds"]
        if duration is None or duration <= 0:
            raise SceneVideoPermanentError(
                "The KIE deliverable reported an invalid duration.",
                code="invalid_video",
            )
        requested = float(job.duration_seconds)
        tolerance = max(2.0, requested * 0.15)
        if abs(duration - requested) > tolerance:
            raise SceneVideoPermanentError(
                f"The KIE deliverable is {duration:.1f}s but {requested:.1f}s was requested; "
                "the generated video does not match the paid single-shot contract.",
                code="invalid_video",
            )
        return probe

    def _plan_real_product_overlays(
        self,
        project: ContentProject,
        jobs: list[SceneVideoJob],
        *,
        output_dir_hint: uuid.UUID,
        width: int,
        height: int,
    ) -> tuple[list[tuple[Path, float, float]], Path | None]:
        """Plan real-UI screenshot overlays and the Persian subtitle file.

        Screenshots live under ``screenshots_root/<platform>/`` and are shown
        during their scene's time window (scene i -> screenshot i, cycling when
        fewer screenshots exist than scenes). Deterministic local stitching
        only: the AI footage stays, the real product UI is drawn on top of it.
        """
        overlays: list[tuple[Path, float, float]] = []
        subtitle_path: Path | None = None

        platform = (project.scene_options or {}).get("screenshots_platform")
        root = self._screenshots_root
        if platform and root is not None and root.is_dir():
            platform_dir = root / str(platform)
            if platform_dir.is_dir():
                images = sorted(
                    item for item in platform_dir.iterdir()
                    if item.is_file() and item.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}
                )
                if images:
                    ordered = sorted(jobs, key=lambda item: item.scene_index)
                    start = 0.0
                    for index, job in enumerate(ordered):
                        end = start + float(job.duration_seconds)
                        overlays.append((images[index % len(images)], start, end))
                        start = end

        narration = (project.script_text or "").strip()
        if narration:
            from app.services.subtitle_service import build_ass_for_script

            total = sum(float(job.duration_seconds) for job in jobs)
            ass = build_ass_for_script(narration, total_seconds=total, width=width, height=height)
            subtitle_path = Path(tempfile.gettempdir()) / f"vandad-subs-{output_dir_hint}.ass"
            subtitle_path.write_text(ass, encoding="utf-8")
        return overlays, subtitle_path

    async def _run_assembly_ffmpeg(self, command: tuple[str, ...]) -> None:
        """Run one assembly ffmpeg command; permanent failures carry detail."""
        from app.services.video_assembly import SceneVideoPermanentError, SceneVideoTransientError

        try:
            await run_ffmpeg(command)
        except SceneVideoTransientError as exc:
            raise SceneVideoTransientError(exc.message, code=exc.code) from exc
        except SceneVideoPermanentError as exc:
            raise SceneVideoPermanentError(exc.message, code=exc.code) from exc

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
        if job.provider == "kie":
            # KIE has no documented idempotency key. Never auto-submit a second
            # paid task after an accepted task reaches a failed terminal state.
            job.submission_metadata = {
                **(job.submission_metadata or {}),
                "failed_provider_job_id": job.provider_job_id,
                "submission_outcome": "provider_failed_no_auto_retry",
            }
            job.provider_job_id = None
            job.provider_status = None
            job.attempts = job.max_attempts
            job.status = SceneVideoJobStatus.FAILED.value
            return
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
