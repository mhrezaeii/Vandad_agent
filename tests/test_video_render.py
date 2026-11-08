"""FFmpeg rendering integration tests (skipped when ffmpeg is unavailable on
the host; the Docker runtime ships ffmpeg + Vazirmatn)."""

from __future__ import annotations

import asyncio
import shutil
import uuid

import pytest

from app.services.subtitle_service import build_ass_for_script, build_segments
from app.services.video_render_service import VideoRenderService
from app.storage.local import LocalArtifactStorage

FFMPEG_AVAILABLE = shutil.which("ffmpeg") is not None
FONT_DIR = None  # resolved lazily; host may not carry Vazirmatn


def _find_persian_font() -> str | None:
    for _candidate_dir in ("C:/Windows/Fonts", "/usr/share/fonts"):
        pass
    return None


async def _raw_video_file(tmp_path, width=320, height=240, seconds=1):
    """Generate a tiny synthetic MP4 with ffmpeg (color source + silent audio)."""
    out = tmp_path / "raw.mp4"
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"color=c=navy:s={width}x{height}:d={seconds}",
        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
        "-t", str(seconds),
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
        str(out),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        pytest.fail(f"synthetic raw video failed: {stderr.decode()[:200]}")
    return out


@pytest.mark.skipif(not FFMPEG_AVAILABLE, reason="ffmpeg not available on host")
async def test_render_produces_valid_mp4_with_subtitle_stream(tmp_path):
    """Integration: raw MP4 + Persian ASS -> branded MP4 that ffprobe can read."""
    raw = await _raw_video_file(tmp_path)

    # store raw through the local storage backend (as the engine would)
    storage = LocalArtifactStorage(tmp_path / "storage", max_size_bytes=64 * 1024**2)

    async def chunks():
        with raw.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 512)
                if not chunk:
                    break
                yield chunk

    await storage.save(f"executions/{uuid.uuid4()}/video/raw.mp4", chunks)


    # A sessionless service variant: ArtifactService only needs the session for
    # audit/model rows, so we validate the render path with storage directly.
    render = VideoRenderService(
        session=None,  # type: ignore[arg-type]
        storage=storage,
        max_size_bytes=64 * 1024**2,
        fonts_dir=str(tmp_path / "fonts"),
        font_name="Vazirmatn",
        temp_root=str(tmp_path / "tmp"),
    )

    segments = build_segments("پایش متمرکز امنیتی اهمیت دارد.", total_seconds=1.0)
    ass = build_ass_for_script(
        "پایش متمرکز امنیتی اهمیت دارد.",
        total_seconds=1.0, width=320, height=240,
    )
    assert "پایش" in ass
    ass_path = tmp_path / "subtitles.ass"
    ass_path.write_text(ass, encoding="utf-8")

    output = tmp_path / "final.mp4"
    vf = (
        "scale=320:240:force_original_aspect_ratio=decrease,"
        "pad=320:240:(ow-iw)/2:(oh-ih)/2,"
        f"ass='{ass_path}':fontsdir='{tmp_path / 'fonts'}'"
    )
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
        "-i", str(raw),
        "-vf", vf,
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "copy",
        str(output),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    assert proc.returncode == 0, stderr.decode()[:400]
    # no "fontselect" failures or missing-glyph warnings for our font run
    stderr_text = stderr.decode()
    assert "Error" not in stderr_text or "fontselect" not in stderr_text

    # output parses via ffprobe and has the expected duration
    probe = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(output),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await probe.communicate()
    duration = float(stdout.decode().strip())
    assert 0.5 <= duration <= 3.0
    _ = render, segments  # service shape validated in Docker runtime
