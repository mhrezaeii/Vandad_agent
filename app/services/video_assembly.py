"""Local FFmpeg scene-clip assembly.

Multi-scene projects whose provider cannot compose a final storyboard video
are assembled locally: the validated scene clips are concatenated into the
final vertical deliverable. This is deterministic media stitching — no
creative generation happens here, so it needs no AI provider.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from app.integrations.video.base import SceneVideoPermanentError, SceneVideoTransientError


def ffmpeg_binary() -> str:
    """Return the ffmpeg executable path; fail closed when it is absent."""
    binary = shutil.which("ffmpeg")
    if binary is None:
        raise SceneVideoPermanentError(
            "ffmpeg is not installed in this environment; local assembly is unavailable.",
            code="ffmpeg_unavailable",
        )
    return binary


async def concat_scene_clips(
    clip_paths: tuple[Path, ...], *, output_path: Path, width: int, height: int
) -> int:
    """Concatenate scene clips into one MP4; returns the output size in bytes.

    The clips come from one provider/model, so they normally share codec and
    geometry; the scale/pad/fps filter chain still normalises any drift so a
    single continuous MP4 is always produced.
    """
    if not clip_paths:
        raise SceneVideoPermanentError(
            "No scene clips were provided for assembly.", code="assembly_empty"
        )
    for path in clip_paths:
        if not path.is_file():
            raise SceneVideoPermanentError(
                "A scene clip file is missing on disk.", code="assembly_clip_missing"
            )
    ffmpeg = ffmpeg_binary()

    output_path.parent.mkdir(parents=True, exist_ok=True)
    concat_list = output_path.with_suffix(".concat.txt")
    concat_list.write_text(
        "".join(f"file '{path.as_posix()}'\n" for path in clip_paths),
        encoding="utf-8",
    )
    command = (
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", str(concat_list),
        "-vf",
        (
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,fps=30,format=yuv420p"
        ),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-movflags", "+faststart", "-an",
        str(output_path),
    )
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _stdout, stderr = await process.communicate()
    except OSError as exc:
        raise SceneVideoTransientError(
            "ffmpeg could not be started for local assembly.",
            code="assembly_spawn_failed",
        ) from exc
    finally:
        concat_list.unlink(missing_ok=True)

    if process.returncode != 0:
        detail = (stderr or b"").decode("utf-8", "replace").strip()[-400:]
        raise SceneVideoPermanentError(
            f"ffmpeg assembly failed{(f' ({detail})' if detail else '')}.",
            code="assembly_failed",
        )
    size = output_path.stat().st_size
    if size <= 0:
        raise SceneVideoPermanentError(
            "ffmpeg produced an empty output file.", code="assembly_empty_output"
        )
    return size
