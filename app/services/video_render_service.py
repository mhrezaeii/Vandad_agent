"""FFmpeg rendering service: branded vertical MP4 with Persian subtitles.

Pipeline per render:
1. Stream the raw artifact into an isolated temp directory (ffmpeg needs a
   seekable input file).
2. Emit a Persian ASS subtitle document (libass + HarfBuzz shape RTL text;
   a Persian-capable font directory must be configured).
3. Run ffmpeg with a bounded timeout:
   - scale/pad to the brand canvas (1080x1920 by default)
   - burn the ASS subtitles
   - optional logo overlay (PNG artifact referenced in brand visual_config)
   - loudnorm audio normalization
   - H.264 + AAC MP4 (faststart for web playback)
4. Stream the rendered file into ArtifactStorage and clean the temp dir.

Every stage is isolated under one temp directory that is removed in a
``finally`` block; ffmpeg failures surface as ArtifactStorageError-free
RuntimeErrors with sanitized messages.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ArtifactKind, WorkflowArtifact
from app.services.artifact_service import ArtifactService
from app.services.subtitle_service import build_ass_for_script
from app.storage.base import ArtifactStorage

FFMPEG_READ_CHUNK = 1024 * 1024

logger = logging.getLogger("app.video_render")


class RenderError(RuntimeError):
    """Raised when ffmpeg fails (message sanitized for clients)."""


class VideoRenderService:
    def __init__(
        self,
        session: AsyncSession,
        storage: ArtifactStorage,
        *,
        max_size_bytes: int = 2 * 1024**3,
        ffmpeg_bin: str = "ffmpeg",
        ffprobe_bin: str = "ffprobe",
        fonts_dir: str = "fonts",
        font_name: str = "Vazirmatn",
        temp_root: str | None = None,
    ) -> None:
        self.session = session
        self.storage = storage
        self.artifact_service = ArtifactService(session, storage, max_size_bytes)
        self._ffmpeg_bin = ffmpeg_bin
        self._ffprobe_bin = ffprobe_bin
        self._fonts_dir = Path(fonts_dir)
        self._font_name = font_name
        self._temp_root = Path(temp_root) if temp_root else Path("data/tmp")
        self._temp_root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ public
    async def render_branded_video(
        self,
        *,
        execution_id: uuid.UUID,
        company_id: uuid.UUID,
        raw_artifact_id: uuid.UUID,
        script_text: str,
        brand_name: str,
        width: int,
        height: int,
        visual_config: dict[str, Any],
        timeout_seconds: float = 1800.0,
        estimated_duration_seconds: float | None = None,
    ) -> tuple[WorkflowArtifact, Path]:
        """Render the final branded MP4 and store it; returns (artifact, tmp_dir)."""
        raw_artifact = await self.artifact_service.get_artifact(
            raw_artifact_id, company_id=company_id
        )

        work_dir = self._temp_root / f"render-{uuid.uuid4().hex}"
        work_dir.mkdir(parents=True, exist_ok=True)
        try:
            raw_path = work_dir / "raw-video.mp4"
            await self._materialize(raw_artifact.storage_key, raw_path)

            ass_path = work_dir / "subtitles.ass"
            total_seconds = estimated_duration_seconds or await self._probe_duration(raw_path)
            ass_content = build_ass_for_script(
                script_text,
                total_seconds=total_seconds,
                width=width,
                height=height,
                font_name=self._font_name,
            )
            ass_path.write_text(ass_content, encoding="utf-8")

            output_path = work_dir / "final.mp4"
            await self._run_ffmpeg(
                raw_path=raw_path,
                ass_path=ass_path,
                output_path=output_path,
                width=width,
                height=height,
                visual_config=visual_config,
                timeout_seconds=timeout_seconds,
            )

            stored = await self._store_output(
                execution_id=execution_id,
                company_id=company_id,
                output_path=output_path,
                brand_name=brand_name,
            )
            return stored, work_dir
        finally:
            await self._cleanup(work_dir)

    # ---------------------------------------------------------------- internal
    async def _materialize(self, storage_key: str, destination: Path) -> None:
        """Stream an artifact from storage into a local temp file."""
        with destination.open("wb") as handle:
            async for chunk in self.storage.open(storage_key):
                await asyncio.to_thread(handle.write, chunk)

    async def _probe_duration(self, video_path: Path) -> float:
        proc = await asyncio.create_subprocess_exec(
            self._ffprobe_bin,
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(video_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        if proc.returncode != 0:
            return 30.0  # conservative default for narration pacing
        try:
            return max(float(stdout.decode().strip()), 1.0)
        except ValueError:
            return 30.0

    async def _run_ffmpeg(
        self,
        *,
        raw_path: Path,
        ass_path: Path,
        output_path: Path,
        width: int,
        height: int,
        visual_config: dict[str, Any],
        timeout_seconds: float,
    ) -> None:
        fonts_dir = self._fonts_dir.resolve()
        ass_arg = str(ass_path).replace("\\", "/").replace(":", "\\:")
        vf = (
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,"
            f"ass='{ass_arg}':fontsdir='{fonts_dir}'"
        )
        # Logo overlay (optional): a local PNG path may be configured in
        # visual_config["logo_path"] (mounted into the worker container).
        overlay_path = visual_config.get("logo_path")
        inputs: list[str] = ["-i", str(raw_path)]
        if overlay_path and Path(str(overlay_path)).is_file():
            inputs += ["-i", str(overlay_path)]
            vf += ";[1:v]scale=iw*0.18:-1[logo];[0:v][logo]overlay=W-w-40:40[vout]"
            args = [
                self._ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error",
                *inputs,
                "-map", "[vout]", "-map", "0:a?",
                "-vf", vf,
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-pix_fmt", "yuv420p",
                "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
                "-c:a", "aac", "-b:a", "160k",
                "-movflags", "+faststart",
                str(output_path),
            ]
        else:
            args = [
                self._ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error",
                *inputs,
                "-vf", vf,
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-pix_fmt", "yuv420p",
                "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
                "-c:a", "aac", "-b:a", "160k",
                "-movflags", "+faststart",
                str(output_path),
            ]

        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
        except TimeoutError as exc:
            proc.kill()
            raise RenderError("FFmpeg render timed out.") from exc
        if proc.returncode != 0:
            stderr_text = stderr.decode("utf-8", errors="replace")
            logger.error("ffmpeg failed (%d): %s", proc.returncode, stderr_text[-2000:])
            raise RenderError("FFmpeg rendering failed; see worker logs for details.")

    async def _store_output(
        self, *, execution_id: uuid.UUID, company_id: uuid.UUID, output_path: Path, brand_name: str
    ) -> WorkflowArtifact:
        async def _chunks():
            with output_path.open("rb") as handle:
                while True:
                    chunk = await asyncio.to_thread(handle.read, FFMPEG_READ_CHUNK)
                    if not chunk:
                        break
                    yield chunk

        return await self.artifact_service.store_artifact(
            execution_id,
            company_id=company_id,
            kind=ArtifactKind.VIDEO.value,
            content_type="video/mp4",
            chunks=_chunks(),
            filename=f"{brand_name.lower()}-final-{uuid.uuid4().hex[:8]}.mp4",
        )

    async def _cleanup(self, work_dir: Path) -> None:
        def _rmtree(path: Path) -> None:
            import shutil

            shutil.rmtree(path, ignore_errors=True)

        await asyncio.to_thread(_rmtree, work_dir)
