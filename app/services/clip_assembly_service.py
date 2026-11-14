"""Assemble a final deliverable from operator-provided clips.

For clips produced outside the paid pipeline (Google Flow free quota, Hoosha,
any external source): drop them into ``assets/clips/<project_id>/`` named
``01.mp4``, ``02.mp4``, ... in playback order. The service reads them in
order, attaches the scenario's per-scene Persian narration (TTS), burns
subtitles, applies platform-screenshot overlays, appends the branded end
card, and stores the final artifact. No paid provider is involved.
"""

from __future__ import annotations

import tempfile
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.tts import TtsSettings
from app.integrations.video import SceneVideoProviderRegistry
from app.integrations.video.base import SceneVideoPermanentError
from app.models import (
    ArtifactKind,
    ContentBrand,
    ContentProject,
    ProjectStatus,
    WorkflowExecution,
    WorkflowStatus,
)
from app.services.artifact_service import ArtifactService
from app.services.branding import render_end_card
from app.services.video_assembly import (
    build_audio_concat_args,
    build_concat_args,
    build_endcard_video_args,
    build_narration_mux_args,
    build_scene_audio_fit_args,
    build_silence_args,
    clip_has_audio,
    concat_scene_clips,
    ffmpeg_binary,
    run_ffmpeg,
)
from app.storage.base import ArtifactStorage

CLIP_ASSEMBLY_WORKFLOW_NAME = "vandad.clip.assembly.v1"
_CLIP_SUFFIXES = {".mp4", ".mov", ".webm"}


class ClipAssemblyError(RuntimeError):
    """Client-safe assembly failure."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


class ClipAssemblyService:
    def __init__(
        self,
        session: AsyncSession,
        registry: SceneVideoProviderRegistry,
        storage: ArtifactStorage,
        *,
        tts_settings: TtsSettings | None = None,
        screenshots_root: str | Path | None = None,
        clips_root: str | Path | None = None,
        end_card_seconds: float = 3.0,
    ) -> None:
        self.session = session
        self.registry = registry
        self.storage = storage
        self._tts_settings = tts_settings
        self._screenshots_root = Path(screenshots_root) if screenshots_root else None
        self._clips_root = Path(clips_root) if clips_root else None
        self._end_card_seconds = float(end_card_seconds)

    # ------------------------------------------------------------------ public
    async def assemble(
        self,
        project: ContentProject,
        *,
        execution_id: uuid.UUID,
        width: int,
        height: int,
    ) -> tuple[Any, dict[str, Any]]:
        ffmpeg = ffmpeg_binary()
        clips = self._collect_clips(project)
        scenes = [
            scene for scene in (project.scenario or {}).get("scenes", [])
            if isinstance(scene, dict)
        ]
        if not scenes:
            raise ClipAssemblyError(
                "The project has no scenario to narrate the clips.", code="clips_scenario_missing"
            )
        artifact_service = ArtifactService(
            self.session, self.storage, max_size_bytes=2 * 1024**3
        )
        workdir = Path(tempfile.mkdtemp(prefix="vandad-clips-"))
        try:
            # Segment durations: each user clip covers one scenario scene; the
            # end card closes the video.
            durations = [self._clip_seconds(ffmpeg, clip) for clip in clips]
            end_card = workdir / "end-card.png"
            brand = await self.session.get(ContentBrand, project.brand_id)
            render_end_card(
                output_path=end_card, width=width, height=height,
                title=(brand.display_name if brand else "") or "Vandefence",
                tagline=str(((brand.visual_config or {}).get("endcard_tagline")) or ""),
                site_url=str(((brand.visual_config or {}).get("site_url")) or ""),
            )
            endcard_video = workdir / "end-card.mp4"
            await run_ffmpeg(build_endcard_video_args(
                ffmpeg=ffmpeg, image_path=end_card, output_path=endcard_video,
                seconds=self._end_card_seconds, width=width, height=height,
            ))

            # Stage 1: normalize + concatenate the user clips.
            concat_list = workdir / "clips-list.txt"
            concat_list.write_text(
                "".join("file '" + c.as_posix() + "'" + chr(10) for c in clips),
                encoding="utf-8",
            )
            clips_full = workdir / "clips-full.mp4"
            clips_with_audio = all(clip_has_audio(c) for c in clips)
            await run_ffmpeg(build_concat_args(
                ffmpeg=ffmpeg, concat_list=concat_list, output_path=clips_full,
                width=width, height=height, with_audio=clips_with_audio,
            ))

            # Stage 2: overlays + subtitles on the clips portion only.
            decorated = clips_full
            overlays = self._plan_overlays(project, clips, durations)
            subtitle_path = self._plan_subtitles(project, scenes, durations, workdir, width, height)
            from app.services.video_assembly import build_finish_args  # noqa: PLC0415

            finish_args = build_finish_args(
                ffmpeg=ffmpeg, source_path=clips_full, output_path=workdir / "decorated.mp4",
                width=width, height=height, overlays=tuple(overlays),
                subtitle_path=subtitle_path, with_audio=clips_with_audio,
            )
            if finish_args is not None:
                await run_ffmpeg(finish_args)
                decorated = workdir / "decorated.mp4"

            # Stage 3: append the end card.
            merged_list = workdir / "merged-list.txt"
            merged_list.write_text(
                "".join("file '" + p.as_posix() + "'" + chr(10)
                        for p in (decorated, endcard_video)),
                encoding="utf-8",
            )
            merged = workdir / "merged.mp4"
            await run_ffmpeg(build_concat_args(
                ffmpeg=ffmpeg, concat_list=merged_list, output_path=merged,
                width=width, height=height, with_audio=True,
            ))

            # Stage 4: narration — one fitted track per clip + end-card silence.
            narration = await self._narration_track(
                ffmpeg, workdir, clips, scenes, durations, self._end_card_seconds
            )
            final = merged
            if narration is not None:
                final = workdir / f"vandad-final-{project.id}.mp4"
                await run_ffmpeg(build_narration_mux_args(
                    ffmpeg=ffmpeg, video_path=merged, narration_audio_path=narration,
                    output_path=final,
                ))

            preview = bytearray()
            async def chunks(path=final):
                with path.open("rb") as handle:
                    while True:
                        block = handle.read(1024 * 1024)
                        if not block:
                            break
                        if len(preview) < 64 * 1024:
                            preview.extend(block[: 64 * 1024 - len(preview)])
                        yield block

            artifact = await artifact_service.store_artifact(
                execution_id,
                company_id=project.company_id,
                kind=ArtifactKind.VIDEO.value,
                content_type="video/mp4",
                chunks=chunks(),
                filename=f"vandad-final-{project.id}.mp4",
            )
        except SceneVideoPermanentError as exc:
            raise ClipAssemblyError(exc.message, code=exc.code) from exc
        finally:
            import shutil  # noqa: PLC0415

            shutil.rmtree(workdir, ignore_errors=True)

        metadata = {
            "service": "clip_assembly",
            "mode": "external_clips",
            "production_path": "operator_clips",
            "clip_count": len(clips),
            "end_card_seconds": self._end_card_seconds,
            "narration": "tts" if narration is not None else "silent",
            "artifact_id": str(artifact.id),
            "size_bytes": artifact.size_bytes,
            "sha256": artifact.sha256,
        }
        project.final_artifact_id = artifact.id
        project.assembly_metadata = metadata
        project.status = ProjectStatus.COMPLETED.value
        project.error_message = None
        execution = await self.session.get(WorkflowExecution, execution_id)
        if execution is not None:
            execution.status = WorkflowStatus.COMPLETED.value
            execution.completed_at = __import__("datetime").datetime.now(__import__("datetime").UTC)
            execution.output_metadata = {"mode": "external_clips", "clips": len(clips)}
        await self.session.flush()
        return artifact, metadata

    # ----------------------------------------------------------------- helpers
    def _collect_clips(self, project: ContentProject) -> list[Path]:
        if self._clips_root is None:
            raise ClipAssemblyError("Clip storage root is not configured.", code="clips_root_missing")
        clip_dir = self._clips_root / str(project.id)
        if not clip_dir.is_dir():
            raise ClipAssemblyError(
                "No clips folder exists for this project "
                f"({self._clips_root.name}/{project.id}); add 01.mp4, 02.mp4, ...",
                code="clips_folder_missing",
            )
        clips = sorted(
            (item for item in clip_dir.iterdir()
             if item.is_file() and item.suffix.lower() in _CLIP_SUFFIXES),
            key=lambda item: item.name,
        )
        if not clips:
            raise ClipAssemblyError(
                "The clips folder is empty; add 01.mp4, 02.mp4, ...",
                code="clips_empty",
            )
        return clips

    @staticmethod
    def _clip_seconds(ffmpeg: str, clip: Path) -> float:
        from app.services.video_assembly import ffprobe_binary  # noqa: PLC0415

        probe = ffprobe_binary()
        if probe is None:
            raise ClipAssemblyError(
                "ffprobe is not installed; clip timing is unavailable.", code="clips_probe_missing"
            )
        import subprocess  # noqa: PLC0415

        result = subprocess.run(
            [probe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(clip)],
            capture_output=True, timeout=60, check=False,
        )
        try:
            return float(result.stdout.strip() or "0")
        except ValueError as exc:
            raise ClipAssemblyError(
                "A clip's duration could not be read.", code="clips_probe_failed"
            ) from exc

    def _plan_overlays(
        self, project: ContentProject, clips: list[Path], durations: list[float]
    ) -> list[tuple[Path, float, float]]:
        platform = (project.scene_options or {}).get("screenshots_platform")
        if not platform or self._screenshots_root is None:
            return []
        platform_dir = self._screenshots_root / str(platform)
        if not platform_dir.is_dir():
            return []
        images = sorted(
            item for item in platform_dir.iterdir()
            if item.is_file() and item.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}
        )
        if not images:
            return []
        overlays: list[tuple[Path, float, float]] = []
        start = 0.0
        for index, seconds in enumerate(durations):
            overlays.append((images[index % len(images)], start, start + seconds))
            start += seconds
        return overlays

    @staticmethod
    def _plan_subtitles(
        project: ContentProject,
        scenes: list[dict[str, Any]],
        durations: list[float],
        workdir: Path,
        width: int,
        height: int,
    ):
        narrations = [str(scene.get("narration") or "").strip() for scene in scenes]
        lines = [n for n in narrations if n]
        if not lines:
            return None
        from app.services.subtitle_service import build_ass_for_script  # noqa: PLC0415

        script = " ".join(lines)
        total = float(sum(durations))
        ass = build_ass_for_script(script, total_seconds=max(1.0, total),
                                   width=width, height=height)
        path = workdir / "narration.ass"
        path.write_text(ass, encoding="utf-8")
        return path

    async def _narration_track(
        self,
        ffmpeg: str,
        workdir: Path,
        clips: list[Path],
        scenes: list[dict[str, Any]],
        durations: list[float],
        end_card_seconds: float,
    ) -> Path | None:
        tts = self._tts_settings
        if tts is None or not tts.provider:
            return None
        fitted: list[Path] = []
        for index, seconds in enumerate(durations):
            narration = str(scenes[index].get("narration") or "").strip() if index < len(scenes) else ""
            if narration:
                raw = workdir / f"raw-{index}.mp3"
                from app.integrations.tts import synthesize  # noqa: PLC0415
                from app.integrations.tts import TextToSpeechError  # noqa: PLC0415

                try:
                    raw.write_bytes(await synthesize(narration, tts))
                except TextToSpeechError as exc:
                    raise ClipAssemblyError(str(exc), code=exc.code) from exc
                fitted_path = workdir / f"fit-{index}.wav"
                await run_ffmpeg(build_scene_audio_fit_args(
                    ffmpeg=ffmpeg, source_audio=raw, output_path=fitted_path,
                    scene_seconds=seconds,
                ))
            else:
                fitted_path = workdir / f"silence-{index}.wav"
                await run_ffmpeg(build_silence_args(
                    ffmpeg=ffmpeg, seconds=seconds, output_path=fitted_path
                ))
            fitted.append(fitted_path)
        tail = workdir / "silence-endcard.wav"
        await run_ffmpeg(build_silence_args(
            ffmpeg=ffmpeg, seconds=end_card_seconds, output_path=tail
        ))
        fitted.append(tail)
        concat_list = workdir / "narration-list.txt"
        concat_list.write_text(
            "".join("file '" + p.as_posix() + "'" + chr(10) for p in fitted),
            encoding="utf-8",
        )
        narration_full = workdir / "narration-full.wav"
        await run_ffmpeg(build_audio_concat_args(
            ffmpeg=ffmpeg, concat_list=concat_list, output_path=narration_full
        ))
        return narration_full
