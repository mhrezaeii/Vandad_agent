"""FFmpeg assembly for the scene-based pipeline.

Pipeline per assembly (all work isolated in one temp directory):

1. Stream every downloaded scene artifact from ``ArtifactStorage`` to disk.
2. Normalise each clip to the output canvas, frame rate and SAR, padding
   shorter clips with their last frame so scene timing is preserved.
3. Concatenate the normalised clips in scene order.
4. Build one Persian ASS subtitle document from the scenario timings.
5. Burn subtitles and (optionally) overlay the brand logo.
6. Build the audio track from an optional narration artifact, optionally mixed
   with brand music that the operator explicitly marked as licensed.
7. Mux video + audio into H.264/AAC MP4 and stream it back into storage.

No narration provider is bundled: when no narration artifact is supplied the
track is silent and the assembly metadata records that fact instead of
pretending narration was generated.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError
from app.models import ContentBrand, ContentProject, SceneVideoJob, WorkflowArtifact
from app.schemas.scenario import Scenario
from app.services.artifact_service import ArtifactService
from app.services.subtitle_service import SubtitleSegment, build_ass_subtitle, build_segments
from app.storage.base import ArtifactStorage

DEFAULT_WIDTH = 1080
DEFAULT_HEIGHT = 1920
DEFAULT_FPS = 30


class SceneAssemblyError(Exception):
    """Assembly failed (message is client-safe; never contains secrets)."""

    def __init__(self, message: str, *, code: str = "scene_assembly_failed") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


def build_scene_subtitles(
    scenario: Scenario,
    *,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    font_name: str = "Vazirmatn",
    font_size: int | None = None,
    primary_color: str = "&H00FFFFFF",
    outline_color: str = "&H00101010",
) -> str:
    """Build a single ASS document whose timings follow the scene plan."""
    segments: list[SubtitleSegment] = []
    cursor = 0.0
    for scene in scenario.scenes:
        text = scene.subtitle_text or scene.narration
        for segment in build_segments(text, scene.duration_seconds):
            segments.append(
                SubtitleSegment(
                    text=segment.text,
                    start_seconds=round(cursor + segment.start_seconds, 3),
                    end_seconds=round(cursor + segment.end_seconds, 3),
                )
            )
        cursor += scene.duration_seconds
    return build_ass_subtitle(
        segments,
        width=width,
        height=height,
        font_name=font_name,
        font_size=font_size,
        primary_color=primary_color,
        outline_color=outline_color,
    )


class SceneAssemblyService:
    def __init__(
        self,
        session: AsyncSession,
        storage: ArtifactStorage,
        *,
        ffmpeg_bin: str = "ffmpeg",
        ffprobe_bin: str = "ffprobe",
        fonts_dir: str = "/usr/share/fonts",
        font_name: str = "Vazirmatn",
        max_size_bytes: int = 2 * 1024**3,
    ) -> None:
        self.session = session
        self.storage = storage
        self.ffmpeg_bin = ffmpeg_bin
        self.ffprobe_bin = ffprobe_bin
        self.fonts_dir = fonts_dir
        self.font_name = font_name
        self.max_size_bytes = max_size_bytes

    async def assemble(
        self,
        *,
        execution_id: uuid.UUID,
        company_id: uuid.UUID,
        project: ContentProject,
        brand: ContentBrand,
        scene_jobs: list[SceneVideoJob],
        narration_artifact_id: uuid.UUID | None = None,
        width: int = DEFAULT_WIDTH,
        height: int = DEFAULT_HEIGHT,
        fps: int = DEFAULT_FPS,
        timeout_seconds: float = 1800.0,
    ) -> tuple[WorkflowArtifact, dict[str, Any]]:
        ordered = sorted(scene_jobs, key=lambda job: job.scene_index)
        if not ordered:
            raise ConflictError("No scene jobs to assemble.")
        if any(job.artifact_id is None for job in ordered):
            raise ConflictError("Every scene must be downloaded before assembly.")
        scenario = self._scenario(project)
        self._require_binaries()

        temp_dir = Path(tempfile.mkdtemp(prefix="vandad-scenes-"))
        try:
            scene_paths: list[Path] = []
            for job in ordered:
                target = temp_dir / f"scene-{job.scene_index:02d}.mp4"
                await self._download_artifact(
                    job.artifact_id, company_id=company_id, destination=target
                )
                scene_paths.append(target)

            normalized: list[Path] = []
            for job, source in zip(ordered, scene_paths, strict=True):
                target = temp_dir / f"norm-{job.scene_index:02d}.mp4"
                await self._normalize_scene(
                    source,
                    target,
                    duration_seconds=job.duration_seconds,
                    width=width,
                    height=height,
                    fps=fps,
                    timeout_seconds=timeout_seconds,
                    cwd=temp_dir,
                )
                normalized.append(target)

            concatenated = temp_dir / "concatenated.mp4"
            await self._concatenate(normalized, concatenated, timeout_seconds=timeout_seconds, cwd=temp_dir)

            total_duration = round(sum(job.duration_seconds for job in ordered), 3)
            subtitles_path = temp_dir / "subtitles.ass"
            subtitles_path.write_text(
                build_scene_subtitles(
                    scenario,
                    width=width,
                    height=height,
                    font_name=self.font_name,
                    font_size=(brand.visual_config or {}).get("subtitle_font_size"),
                    primary_color=(brand.visual_config or {}).get(
                        "subtitle_primary_color", "&H00FFFFFF"
                    ),
                    outline_color=(brand.visual_config or {}).get(
                        "subtitle_outline_color", "&H00101010"
                    ),
                ),
                encoding="utf-8",
            )

            silent_video = temp_dir / "video-silent.mp4"
            logo = self._optional_asset(brand, "logo_path")
            await self._render_video(
                concatenated,
                silent_video,
                subtitles_name=subtitles_path.name,
                logo=logo,
                width=width,
                height=height,
                fps=fps,
                timeout_seconds=timeout_seconds,
                cwd=temp_dir,
            )

            audio = temp_dir / "audio.m4a"
            narration_used = narration_artifact_id is not None
            narration_path = None
            if narration_artifact_id is not None:
                narration_path = temp_dir / "narration.bin"
                await self._download_artifact(
                    narration_artifact_id, company_id=company_id, destination=narration_path
                )
            music_path = self._optional_asset(brand, "music_path", require_licensed=True)
            await self._build_audio(
                audio,
                total_duration=total_duration,
                narration_path=narration_path,
                music_path=music_path,
                music_volume=float((brand.visual_config or {}).get("music_volume", 0.12)),
                timeout_seconds=timeout_seconds,
                cwd=temp_dir,
            )

            final_path = temp_dir / "final.mp4"
            await self._mux(
                silent_video,
                audio,
                final_path,
                timeout_seconds=timeout_seconds,
                cwd=temp_dir,
            )
            probe = await self._probe(final_path)

            async def _final_chunks():  # noqa: ANN202 - async generator
                with final_path.open("rb") as handle:
                    while True:
                        chunk = handle.read(1024 * 1024)
                        if not chunk:
                            break
                        yield chunk

            artifact_service = ArtifactService(
                self.session, self.storage, max_size_bytes=self.max_size_bytes
            )
            artifact = await artifact_service.store_artifact(
                execution_id,
                company_id=company_id,
                kind="video",
                content_type="video/mp4",
                chunks=_final_chunks(),
                filename=f"scene-video-{project.id}.mp4",
            )
            metadata = {
                "service": "ffmpeg",
                "mode": "scene",
                "scenes": len(ordered),
                "duration_seconds": total_duration,
                "width": width,
                "height": height,
                "fps": fps,
                "subtitles": "persian_ass",
                "font": self.font_name,
                "narration": "provided" if narration_used else "silent_fallback",
                "music": "licensed" if music_path is not None else "none",
                "logo": logo is not None,
                "probe": probe,
                "artifact_id": str(artifact.id),
                "size_bytes": artifact.size_bytes,
                "sha256": artifact.sha256,
            }
            return artifact, metadata
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    # --------------------------------------------------------------- helpers
    @staticmethod
    def _scenario(project: ContentProject) -> Scenario:
        if not project.scenario:
            raise ConflictError("The project has no scenario to assemble.")
        return Scenario.model_validate(project.scenario)

    def _require_binaries(self) -> None:
        if shutil.which(self.ffmpeg_bin) is None:
            raise SceneAssemblyError(
                "ffmpeg is not available on this host.", code="ffmpeg_unavailable"
            )
        if shutil.which(self.ffprobe_bin) is None:
            raise SceneAssemblyError(
                "ffprobe is not available on this host.", code="ffprobe_unavailable"
            )

    def _optional_asset(
        self, brand: ContentBrand, key: str, *, require_licensed: bool = False
    ) -> Path | None:
        config = brand.visual_config or {}
        raw = config.get(key)
        if not isinstance(raw, str) or not raw.strip():
            return None
        if require_licensed and not config.get("music_licensed", False):
            return None
        path = Path(raw)
        return path if path.is_file() else None

    async def _download_artifact(
        self, artifact_id: uuid.UUID, *, company_id: uuid.UUID, destination: Path
    ) -> None:
        artifact_service = ArtifactService(self.session, self.storage)
        _artifact, stream = await artifact_service.open_artifact_stream(
            artifact_id, company_id=company_id
        )
        with destination.open("wb") as handle:
            async for chunk in stream:
                handle.write(chunk)

    async def _normalize_scene(
        self,
        source: Path,
        target: Path,
        *,
        duration_seconds: float,
        width: int,
        height: int,
        fps: int,
        timeout_seconds: float,
        cwd: Path,
    ) -> None:
        actual = await self._probe(source, cwd=cwd)
        actual_duration = float(actual.get("duration") or 0.0)
        pad = max(0.0, duration_seconds - actual_duration) if actual_duration else 0.0
        filters = [
            f"scale={width}:{height}:force_original_aspect_ratio=decrease",
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black",
            "setsar=1",
            f"fps={fps}",
        ]
        if pad > 0:
            filters.append(f"tpad=stop_mode=clone:stop_duration={pad:.3f}")
        await self._run(
            [
                self.ffmpeg_bin,
                "-y",
                "-i",
                source.name,
                "-vf",
                ",".join(filters),
                "-t",
                f"{duration_seconds:.3f}",
                "-an",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                target.name,
            ],
            cwd=cwd,
            timeout_seconds=timeout_seconds,
        )

    async def _concatenate(
        self, inputs: list[Path], target: Path, *, timeout_seconds: float, cwd: Path
    ) -> None:
        list_path = cwd / "concat.txt"
        list_path.write_text(
            "\n".join(f"file '{path.name}'" for path in inputs) + "\n", encoding="utf-8"
        )
        await self._run(
            [
                self.ffmpeg_bin,
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                list_path.name,
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                target.name,
            ],
            cwd=cwd,
            timeout_seconds=timeout_seconds,
        )

    async def _render_video(
        self,
        source: Path,
        target: Path,
        *,
        subtitles_name: str,
        logo: Path | None,
        width: int,
        height: int,
        fps: int,
        timeout_seconds: float,
        cwd: Path,
    ) -> None:
        ass_arg = subtitles_name.replace("\\", "/")
        ass_filter = f"ass='{ass_arg}'"
        fonts_dir = Path(self.fonts_dir)
        if fonts_dir.is_dir():
            fonts_arg = str(fonts_dir.resolve()).replace("\\", "/").replace(":", "\\:")
            ass_filter += f":fontsdir='{fonts_arg}'"

        command = [self.ffmpeg_bin, "-y", "-i", source.name]
        if logo is not None:
            local_logo = cwd / "brand-logo"
            shutil.copyfile(logo, local_logo)
            command += ["-i", local_logo.name]
            logo_width = max(1, int(width * 0.18))
            filter_complex = (
                f"[0:v]{ass_filter}[bg];"
                f"[1:v]scale={logo_width}:-1[logo];"
                f"[bg][logo]overlay=W-w-40:40[v]"
            )
            command += ["-filter_complex", filter_complex, "-map", "[v]"]
        else:
            command += ["-vf", ass_filter]
        command += [
            "-r",
            str(fps),
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            target.name,
        ]
        await self._run(command, cwd=cwd, timeout_seconds=timeout_seconds)

    async def _build_audio(
        self,
        target: Path,
        *,
        total_duration: float,
        narration_path: Path | None,
        music_path: Path | None,
        music_volume: float,
        timeout_seconds: float,
        cwd: Path,
    ) -> None:
        command = [self.ffmpeg_bin, "-y"]
        if narration_path is not None:
            command += ["-i", narration_path.name]
        else:
            command += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
        if music_path is not None:
            local_music = cwd / "brand-music"
            shutil.copyfile(music_path, local_music)
            command += ["-i", local_music.name]
            command += [
                "-filter_complex",
                f"[0:a]apad,volume=1.0[a0];"
                f"[1:a]volume={music_volume:.3f}[a1];"
                f"[a0][a1]amix=inputs=2:duration=longest:dropout_transition=0[a]",
                "-map",
                "[a]",
            ]
        else:
            command += ["-af", "apad"]
        command += [
            "-t",
            f"{total_duration:.3f}",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-ar",
            "48000",
            target.name,
        ]
        await self._run(command, cwd=cwd, timeout_seconds=timeout_seconds)

    async def _mux(
        self, video: Path, audio: Path, target: Path, *, timeout_seconds: float, cwd: Path
    ) -> None:
        await self._run(
            [
                self.ffmpeg_bin,
                "-y",
                "-i",
                video.name,
                "-i",
                audio.name,
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-shortest",
                "-movflags",
                "+faststart",
                target.name,
            ],
            cwd=cwd,
            timeout_seconds=timeout_seconds,
        )

    async def _probe(self, path: Path, *, cwd: Path | None = None) -> dict[str, Any]:
        process = await asyncio.create_subprocess_exec(
            self.ffprobe_bin,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height,r_frame_rate:format=duration",
            "-of",
            "json",
            path.name if cwd is not None else str(path),
            cwd=str(cwd) if cwd is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _stderr = await process.communicate()
        if process.returncode != 0:
            return {}
        try:
            import json

            payload = json.loads(stdout.decode("utf-8", errors="replace") or "{}")
        except ValueError:
            return {}
        streams = payload.get("streams") or []
        stream = streams[0] if streams else {}
        duration = (payload.get("format") or {}).get("duration")
        return {
            "width": stream.get("width"),
            "height": stream.get("height"),
            "fps": stream.get("r_frame_rate"),
            "duration": float(duration) if duration is not None else None,
        }

    async def _run(
        self, command: list[str], *, cwd: Path, timeout_seconds: float
    ) -> None:
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise SceneAssemblyError(
                f"{command[0]} is not available on this host.", code="ffmpeg_unavailable"
            ) from exc
        try:
            _stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=timeout_seconds
            )
        except TimeoutError as exc:
            process.kill()
            await process.wait()
            raise SceneAssemblyError(
                "The media operation timed out.", code="assembly_timeout"
            ) from exc
        if process.returncode != 0:
            tail = (stderr or b"").decode("utf-8", errors="replace")[-600:]
            raise SceneAssemblyError(
                f"ffmpeg failed (exit {process.returncode}): {tail}",
                code="ffmpeg_failed",
            )


__all__ = [
    "DEFAULT_FPS",
    "DEFAULT_HEIGHT",
    "DEFAULT_WIDTH",
    "SceneAssemblyError",
    "SceneAssemblyService",
    "build_scene_subtitles",
]
