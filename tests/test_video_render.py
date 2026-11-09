"""FFmpeg rendering integration tests.

These exercise the real ``VideoRenderService`` (not a hand-rolled ffmpeg
command): raw artifact -> Persian ASS subtitles -> branded 1080x1920 MP4 ->
ArtifactStorage. Skipped when ffmpeg is unavailable on the host; the Docker
runtime ships ffmpeg + Vazirmatn (``fonts-vazirmatn``).
"""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.artifact_service import ArtifactService
from app.services.subtitle_service import build_ass_for_script, build_segments
from app.services.video_render_service import VideoRenderService
from app.services.workflow_execution_service import WorkflowExecutionService
from app.storage.local import LocalArtifactStorage
from tests.test_worker_hardening import _keybox

FFMPEG_AVAILABLE = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None

PERSIAN_SCRIPT = (
    "شبکه سازمان شما هر ثانیه رویداد امنیتی تولید می‌کند. "
    "پایش متمرکز یعنی دیدن همه این رویدادها در یک نقطه. "
    "Vandefence این تصویر را شفاف می‌کند."
)


def _find_fonts_dir() -> str | None:
    """Return a directory that carries a Persian-capable font, if any."""
    candidates = [
        os.environ.get("VIDEO_FONTS_DIR"),
        "/usr/share/fonts/truetype/vazirmatn",
        "/usr/share/fonts",
        "C:/Windows/Fonts",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_dir():
            return candidate
    return None


FONTS_DIR = _find_fonts_dir()


async def _raw_video_file(tmp_path: Path, width: int = 360, height: int = 640, seconds: float = 2):
    """Generate a tiny synthetic MP4 with ffmpeg (colour source + silent audio)."""
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
        pytest.fail(f"synthetic raw video failed: {stderr.decode()[:300]}")
    return out


async def _ffprobe(path: Path, entry: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", entry, "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    assert proc.returncode == 0
    return stdout.decode().strip()


@pytest.mark.skipif(not FFMPEG_AVAILABLE, reason="ffmpeg/ffprobe not available on host")
async def test_render_branded_video_through_service(
    tmp_path, db_session, engine_pair, workflow_factory, company_and_platform
):
    """Real service render: 1080x1920 MP4 with burned Persian subtitles."""
    company, _platform = company_and_platform
    registry, executor = engine_pair
    workflow = await workflow_factory(
        company.id,
        [{"node_key": "start", "node_type": "trigger.manual", "name": "t", "configuration": {}}],
        [],
    )
    execution_service = WorkflowExecutionService(
        db_session, registry, executor, _keybox(), queue=None
    )
    execution, _created = await execution_service.execute_workflow(
        workflow.id, scope=SimpleNamespace(company_id=company.id), enqueue=False
    )
    await db_session.commit()

    storage = LocalArtifactStorage(tmp_path / "storage", max_size_bytes=128 * 1024**2)
    artifact_service = ArtifactService(db_session, storage)
    raw_path = await _raw_video_file(tmp_path)

    async def _chunks():
        with raw_path.open("rb") as handle:
            while True:
                chunk = handle.read(512 * 1024)
                if not chunk:
                    break
                yield chunk

    raw_artifact = await artifact_service.store_artifact(
        execution.id,
        company_id=company.id,
        kind="video",
        content_type="video/mp4",
        chunks=_chunks(),
        filename="raw.mp4",
    )
    await db_session.commit()

    render = VideoRenderService(
        db_session,
        storage,
        max_size_bytes=128 * 1024**2,
        fonts_dir=FONTS_DIR or str(tmp_path / "missing-fonts"),
        font_name="Vazirmatn",
        temp_root=str(tmp_path / "tmp"),
    )
    final_artifact, work_dir = await render.render_branded_video(
        execution_id=execution.id,
        company_id=company.id,
        raw_artifact_id=raw_artifact.id,
        script_text=PERSIAN_SCRIPT,
        brand_name="Vandefence",
        width=1080,
        height=1920,
        visual_config={
            "fit": "cover",
            "background_color": "#0A1128",
            "subtitle_font_size": 56,
            "subtitle_primary_color": "&H00FFFFFF",
            "subtitle_outline_color": "&H00101010",
        },
        timeout_seconds=180,
    )

    assert final_artifact.content_type == "video/mp4"
    assert final_artifact.size_bytes > 0
    assert final_artifact.sha256
    # The service always removes its isolated temp directory.
    assert not work_dir.exists()

    final_path = tmp_path / "downloaded-final.mp4"
    with final_path.open("wb") as handle:
        async for chunk in storage.open(final_artifact.storage_key):
            handle.write(chunk)

    assert await _ffprobe(final_path, "stream=width") == "1080"
    assert await _ffprobe(final_path, "stream=height") == "1920"

    # Extract a frame so the subtitle burn-in can be inspected in the Docker
    # runtime (a successful exit code alone does not prove correct shaping).
    frame = tmp_path / "frame.png"
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-ss", "0.5", "-i", str(final_path), "-frames:v", "1", str(frame),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()
    assert proc.returncode == 0
    assert frame.is_file() and frame.stat().st_size > 1000


def test_persian_ass_document_has_rtl_style_and_shaping_requirements():
    segments = build_segments(PERSIAN_SCRIPT, total_seconds=30)
    assert len(segments) >= 2
    ass = build_ass_for_script(
        PERSIAN_SCRIPT,
        total_seconds=30,
        width=1080,
        height=1920,
        font_name="Vazirmatn",
        font_size=56,
    )
    assert "Vazirmatn" in ass
    assert "PlayResX: 1080" in ass
    assert "PlayResY: 1920" in ass
    assert "شبکه" in ass
    # Explicit font size + bottom-centred alignment in the style line.
    assert ",Vazirmatn,56," in ass
    assert ",2,60,60," in ass
