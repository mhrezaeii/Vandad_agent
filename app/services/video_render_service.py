"""FFmpeg rendering service: branded vertical MP4 with Persian subtitles.

Pipeline per render (each stage isolated in one temp directory):

1. Stream the raw artifact into the temp directory (ffmpeg needs a seekable
   input file).
2. Burn a Persian ASS subtitle document onto the raw clip. libass (built with
   HarfBuzz + FriBidi) performs RTL shaping at render time; a Persian-capable
   font directory must be configured (the Docker runtime ships
   ``fonts-vazirmatn``).
3. Optionally normalize and prepend/append an intro/outro clip.
4. Optionally overlay a brand logo and mix licensed background music, then
   normalize loudness.
5. Stream the rendered MP4 into ArtifactStorage and clean the temp directory.

Brand visuals are data-driven through ``visual_config`` (colors, logo path and
scale, music path/volume, intro/outro paths, fit mode, subtitle styling), so
no Vandefence-specific logic lives here.

Every stage is isolated under one temp directory that is removed in a
``finally`` block; ffmpeg failures surface as sanitized ``RenderError``s.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ArtifactKind, WorkflowArtifact
from app.services.artifact_service import ArtifactService
from app.services.subtitle_service import build_ass_for_script
from app.storage.base import ArtifactStorage

FFMPEG_READ_CHUNK = 1024 * 1024
DEFAULT_FPS = 25
DEFAULT_AUDIO_RATE = 48000
LOUDNORM = "loudnorm=I=-16:TP=-1.5:LRA=11"

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

            total_seconds = estimated_duration_seconds or await self._probe_duration(raw_path)
            ass_path = work_dir / "subtitles.ass"
            ass_path.write_text(
                build_ass_for_script(
                    script_text,
                    total_seconds=total_seconds,
                    width=width,
                    height=height,
                    font_name=str(visual_config.get("font_name") or self._font_name),
                    font_size=visual_config.get("subtitle_font_size"),
                    primary_color=str(
                        visual_config.get("subtitle_primary_color") or "&H00FFFFFF"
                    ),
                    outline_color=str(
                        visual_config.get("subtitle_outline_color") or "&H00101010"
                    ),
                ),
                encoding="utf-8",
            )

            subbed_path = work_dir / "subbed.mp4"
            await self._render_subbed(
                raw_path=raw_path,
                ass_path=ass_path,
                output_path=subbed_path,
                width=width,
                height=height,
                visual_config=visual_config,
                timeout_seconds=timeout_seconds,
            )

            joined_path = await self._join_intro_outro(
                subbed_path=subbed_path,
                work_dir=work_dir,
                width=width,
                height=height,
                visual_config=visual_config,
                timeout_seconds=timeout_seconds,
            )

            output_path = work_dir / "final.mp4"
            await self._apply_branding(
                input_path=joined_path,
                output_path=output_path,
                width=width,
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

    async def _has_audio(self, media_path: Path) -> bool:
        proc = await asyncio.create_subprocess_exec(
            self._ffprobe_bin,
            "-v", "error",
            "-select_streams", "a",
            "-show_entries", "stream=index",
            "-of", "csv=p=0",
            str(media_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        return proc.returncode == 0 and bool(stdout.decode().strip())

    @staticmethod
    def _fit_filter(width: int, height: int, fit: str, pad_color: str) -> str:
        if fit == "contain":
            return (
                f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color={pad_color}"
            )
        # cover: fill the canvas, center-crop the overflow (social vertical).
        return (
            f"scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height}"
        )

    async def _render_subbed(
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
        ass_filter = f"ass='{ass_arg}'"
        if fonts_dir.is_dir():
            # Explicit font directory wins; otherwise libass falls back to the
            # system fontconfig (the Docker image installs fonts-vazirmatn).
            fonts_arg = str(fonts_dir).replace("\\", "/").replace(":", "\\:")
            ass_filter += f":fontsdir='{fonts_arg}'"
        pad_color = self._pad_color(visual_config)
        fit = str(visual_config.get("fit") or "cover")
        vf = (
            f"{self._fit_filter(width, height, fit, pad_color)},"
            f"fps={DEFAULT_FPS},setsar=1,format=yuv420p,"
            f"{ass_filter}"
        )
        args = [
            self._ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error", "-nostdin",
            "-i", str(raw_path),
        ]
        if await self._has_audio(raw_path):
            args += ["-map", "0:v:0", "-map", "0:a:0", "-af", LOUDNORM, "-c:a", "aac", "-b:a", "160k"]
        else:
            args += [
                "-f", "lavfi", "-i", f"anullsrc=r={DEFAULT_AUDIO_RATE}:cl=stereo",
                "-map", "0:v:0", "-map", "1:a:0", "-shortest",
                "-c:a", "aac", "-b:a", "160k",
            ]
        args += [
            "-vf", vf,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-pix_fmt", "yuv420p", "-r", str(DEFAULT_FPS),
            "-movflags", "+faststart",
            str(output_path),
        ]
        await self._run_ffmpeg(args, timeout_seconds=timeout_seconds, stage="subtitle")

    async def _join_intro_outro(
        self,
        *,
        subbed_path: Path,
        work_dir: Path,
        width: int,
        height: int,
        visual_config: dict[str, Any],
        timeout_seconds: float,
    ) -> Path:
        intro_path = self._existing_asset(visual_config.get("intro_path"))
        outro_path = self._existing_asset(visual_config.get("outro_path"))
        if intro_path is None and outro_path is None:
            return subbed_path

        segments: list[Path] = [subbed_path]
        if intro_path is not None:
            normalized = work_dir / "intro-normalized.mp4"
            await self._normalize_clip(
                intro_path, normalized, width, height, visual_config, timeout_seconds
            )
            segments.insert(0, normalized)
        if outro_path is not None:
            normalized = work_dir / "outro-normalized.mp4"
            await self._normalize_clip(
                outro_path, normalized, width, height, visual_config, timeout_seconds
            )
            segments.append(normalized)

        concat_list = work_dir / "concat.txt"
        concat_list.write_text(
            "\n".join(f"file '{self._concat_escape(path)}'" for path in segments) + "\n",
            encoding="utf-8",
        )
        joined = work_dir / "joined.mp4"
        await self._run_ffmpeg(
            [
                self._ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error", "-nostdin",
                "-f", "concat", "-safe", "0", "-i", str(concat_list),
                "-c", "copy", "-movflags", "+faststart", str(joined),
            ],
            timeout_seconds=timeout_seconds,
            stage="concat",
        )
        return joined

    async def _normalize_clip(
        self,
        source: Path,
        destination: Path,
        width: int,
        height: int,
        visual_config: dict[str, Any],
        timeout_seconds: float,
    ) -> None:
        pad_color = self._pad_color(visual_config)
        fit = str(visual_config.get("fit") or "cover")
        vf = f"{self._fit_filter(width, height, fit, pad_color)},fps={DEFAULT_FPS},setsar=1,format=yuv420p"
        has_audio = await self._has_audio(source)
        args = [
            self._ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error", "-nostdin",
            "-i", str(source),
        ]
        if has_audio:
            args += ["-map", "0:v:0", "-map", "0:a:0", "-af", LOUDNORM, "-c:a", "aac", "-b:a", "160k"]
        else:
            duration = await self._probe_duration(source)
            args += [
                "-f", "lavfi", "-t", f"{duration:.3f}",
                "-i", f"anullsrc=r={DEFAULT_AUDIO_RATE}:cl=stereo",
                "-map", "0:v:0", "-map", "1:a:0", "-shortest",
                "-c:a", "aac", "-b:a", "160k",
            ]
        args += [
            "-vf", vf,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-pix_fmt", "yuv420p", "-r", str(DEFAULT_FPS),
            "-movflags", "+faststart",
            str(destination),
        ]
        await self._run_ffmpeg(args, timeout_seconds=timeout_seconds, stage="intro_outro")

    async def _apply_branding(
        self,
        *,
        input_path: Path,
        output_path: Path,
        width: int,
        visual_config: dict[str, Any],
        timeout_seconds: float,
    ) -> None:
        logo_path = self._existing_asset(visual_config.get("logo_path"))
        music_path = self._existing_asset(visual_config.get("music_path"))
        if logo_path is None and music_path is None:
            # Nothing to add; the subbed/joined file is already the final MP4.
            if input_path != output_path:
                await asyncio.to_thread(shutil.copyfile, input_path, output_path)
            return

        has_audio = await self._has_audio(input_path)
        inputs: list[str] = ["-i", str(input_path)]
        filter_parts: list[str] = []
        next_input = 1

        if logo_path is not None:
            inputs += ["-i", str(logo_path)]
            logo_width = max(int(width * float(visual_config.get("logo_scale", 0.18))), 32)
            margin = int(visual_config.get("logo_margin", 40))
            filter_parts.append(f"[{next_input}:v]scale={logo_width}:-1[logo]")
            filter_parts.append(
                f"[0:v][logo]overlay=W-w-{margin}:{margin}:format=auto[vout]"
            )
            next_input += 1
        else:
            filter_parts.append("[0:v]null[vout]")

        if music_path is not None:
            inputs += ["-stream_loop", "-1", "-i", str(music_path)]
            music_index = next_input
            next_input += 1
            volume = float(visual_config.get("music_volume", 0.12))
            filter_parts.append(f"[{music_index}:a]volume={volume}[music]")
            if has_audio:
                filter_parts.append(
                    f"[0:a]{LOUDNORM}[voice];"
                    f"[voice][music]amix=inputs=2:duration=first:dropout_transition=2[aout]"
                )
            else:
                filter_parts.append(f"[music]{LOUDNORM}[aout]")
            audio_map = "[aout]"
        else:
            if has_audio:
                filter_parts.append(f"[0:a]{LOUDNORM}[aout]")
                audio_map = "[aout]"
            else:
                audio_map = None

        args = [
            self._ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error", "-nostdin",
            *inputs,
            "-filter_complex", ";".join(filter_parts),
            "-map", "[vout]",
        ]
        if audio_map:
            args += ["-map", audio_map, "-c:a", "aac", "-b:a", "160k"]
        args += [
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-pix_fmt", "yuv420p", "-r", str(DEFAULT_FPS),
            "-movflags", "+faststart",
        ]
        if music_path is not None:
            args += ["-shortest"]
        args.append(str(output_path))
        await self._run_ffmpeg(args, timeout_seconds=timeout_seconds, stage="branding")

    async def _run_ffmpeg(self, args: list[str], *, timeout_seconds: float, stage: str) -> None:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        try:
            _stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_seconds
            )
        except TimeoutError as exc:
            proc.kill()
            await proc.wait()
            raise RenderError(f"FFmpeg {stage} stage timed out.") from exc
        if proc.returncode != 0:
            stderr_text = stderr.decode("utf-8", errors="replace")
            logger.error("ffmpeg %s failed (%d): %s", stage, proc.returncode, stderr_text[-2000:])
            raise RenderError(f"FFmpeg {stage} stage failed; see worker logs for details.")

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
            shutil.rmtree(path, ignore_errors=True)

        await asyncio.to_thread(_rmtree, work_dir)

    # ------------------------------------------------------------------ assets
    @staticmethod
    def _existing_asset(value: Any) -> Path | None:
        if not value:
            return None
        path = Path(str(value))
        return path if path.is_file() else None

    @staticmethod
    def _pad_color(visual_config: dict[str, Any]) -> str:
        raw = str(visual_config.get("background_color") or "").strip()
        if raw.startswith("#") and len(raw) == 7:
            return "0x" + raw[1:]
        if raw:
            return raw
        return "black"

    @staticmethod
    def _concat_escape(path: Path) -> str:
        return str(path).replace("\\", "/").replace("'", "'\\''")
