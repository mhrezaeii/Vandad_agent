"""Local FFmpeg scene-clip assembly.

Multi-scene projects whose provider cannot compose a final storyboard video
are assembled locally: the validated scene clips are concatenated into the
final vertical deliverable. Audio-capable providers keep their native
soundtrack; real platform screenshots are overlaid during their scene window
so the deliverable shows the actual product UI, and a Persian ASS subtitle is
burned in. This is deterministic media stitching — no creative generation.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

from app.integrations.video.base import SceneVideoPermanentError, SceneVideoTransientError

# Screenshot overlays cover at most this fraction of the frame width/height.
_MAX_OVERLAY_WIDTH_RATIO = 0.86
_MAX_OVERLAY_HEIGHT_RATIO = 0.86


def ffmpeg_binary() -> str:
    """Return the ffmpeg executable path; fail closed when it is absent."""
    binary = shutil.which("ffmpeg")
    if binary is None:
        raise SceneVideoPermanentError(
            "ffmpeg is not installed in this environment; local assembly is unavailable.",
            code="ffmpeg_unavailable",
        )
    return binary


def ffprobe_binary() -> str | None:
    return shutil.which("ffprobe")


def clip_has_audio(path: Path) -> bool:
    """True when the clip carries at least one audio stream."""
    probe = ffprobe_binary()
    if probe is None:
        # Without ffprobe assume silent; the output stays a valid silent MP4.
        return False
    result = subprocess.run(
        [probe, "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
         "-of", "csv=p=0", str(path)],
        capture_output=True, timeout=60, check=False,
    )
    return bool(result.stdout.strip())


def build_concat_args(
    *,
    ffmpeg: str,
    concat_list: Path,
    output_path: Path,
    width: int,
    height: int,
    with_audio: bool,
) -> tuple[str, ...]:
    """Build the stage-1 concat command (video re-encode, audio preserved)."""
    command: list[str] = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", str(concat_list),
        "-vf",
        (
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,fps=30,format=yuv420p"
        ),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-movflags", "+faststart",
    ]
    if with_audio:
        command += ["-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2"]
    else:
        command += ["-an"]
    command.append(str(output_path))
    return tuple(command)


def build_finish_args(
    *,
    ffmpeg: str,
    source_path: Path,
    output_path: Path,
    width: int,
    height: int,
    overlays: tuple[tuple[Path, float, float], ...],
    subtitle_path: Path | None,
    with_audio: bool,
) -> tuple[str, ...] | None:
    """Build the stage-2 command (screenshot overlays + burned subtitles).

    Returns None when there is nothing to apply; the caller then keeps the
    stage-1 output as the final deliverable.
    """
    if not overlays and subtitle_path is None:
        return None
    overlay_scale_width = int(width * _MAX_OVERLAY_WIDTH_RATIO)
    overlay_scale_height = int(height * _MAX_OVERLAY_HEIGHT_RATIO)
    filters: list[str] = []
    chain = "[0:v]"
    for index, (image, start, end) in enumerate(overlays):
        scaled = f"ov{index}"
        filters.append(
            f"[{index + 1}:v]scale={overlay_scale_width}:{overlay_scale_height}:"
            f"force_original_aspect_ratio=decrease[{scaled}]"
        )
        next_chain = f"ov{index}x"
        filters.append(
            f"{chain}[{scaled}]overlay=(W-w)/2:(H-h)/2:"
            f"enable='between(t,{start:.3f},{end:.3f})'[{next_chain}]"
        )
        chain = next_chain
    if subtitle_path is not None:
        escaped = str(subtitle_path).replace("\\", "/").replace(":", "\:").replace("'", "\'")
        filters.append(f"{chain}subtitles='{escaped}'[subbed]")
        chain = "subbed"
    command: list[str] = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(source_path),
    ]
    for image, _start, _end in overlays:
        command += ["-loop", "1", "-i", str(image)]
    command += [
        "-filter_complex", ";".join(filters),
        "-map", f"[{chain}]",
        "-map", "0:a?" if with_audio else "-an",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
    ]
    if with_audio:
        command += ["-c:a", "aac", "-b:a", "192k"]
    command.append(str(output_path))
    return tuple(command)


async def concat_scene_clips(
    clip_paths: tuple[Path, ...],
    *,
    output_path: Path,
    width: int,
    height: int,
    overlays: tuple[tuple[Path, float, float], ...] = (),
    subtitle_path: Path | None = None,
) -> int:
    """Assemble scene clips into one MP4; returns the output size in bytes.

    The clips come from one provider/model, so they normally share codec and
    geometry; the scale/pad/fps filter chain still normalises any drift. A
    provider-generated soundtrack is preserved when every clip has audio.
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
    for image, _start, _end in overlays:
        if not image.is_file():
            raise SceneVideoPermanentError(
                "A platform screenshot is missing on disk.", code="assembly_overlay_missing"
            )
    ffmpeg = ffmpeg_binary()
    with_audio = all(clip_has_audio(path) for path in clip_paths)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    concat_list = output_path.with_suffix(".concat.txt")
    concat_list.write_text(
        "".join(f"file '{path.as_posix()}'\n" for path in clip_paths),
        encoding="utf-8",
    )
    temp_full = output_path.with_suffix(".full.mp4")
    try:
        await run_ffmpeg(
            build_concat_args(
                ffmpeg=ffmpeg, concat_list=concat_list, output_path=temp_full,
                width=width, height=height, with_audio=with_audio,
            ),
        )
        finish_args = build_finish_args(
            ffmpeg=ffmpeg, source_path=temp_full, output_path=output_path,
            width=width, height=height, overlays=overlays, subtitle_path=subtitle_path,
            with_audio=with_audio,
        )
        if finish_args is None:
            temp_full.replace(output_path)
        else:
            await run_ffmpeg(finish_args)
    finally:
        concat_list.unlink(missing_ok=True)
        temp_full.unlink(missing_ok=True)

    size = output_path.stat().st_size
    if size <= 0:
        raise SceneVideoPermanentError(
            "ffmpeg produced an empty output file.", code="assembly_empty_output"
        )
    return size


def build_silence_args(*, ffmpeg: str, seconds: float, output_path: Path) -> tuple[str, ...]:
    """Generate exact-length silence for scenes without narration."""
    return (
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
        "-t", f"{seconds:.3f}", "-c:a", "pcm_s16le",
        str(output_path),
    )


def build_narration_mux_args(
    *,
    ffmpeg: str,
    video_path: Path,
    narration_audio_path: Path,
    output_path: Path,
) -> tuple[str, ...]:
    """Stage-3: mux one pre-mixed narration track onto the assembled video."""
    return (
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(video_path), "-i", str(narration_audio_path),
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
        "-shortest", "-movflags", "+faststart",
        str(output_path),
    )


def build_scene_audio_fit_args(
    *,
    ffmpeg: str,
    source_audio: Path,
    output_path: Path,
    scene_seconds: float,
) -> tuple[str, ...]:
    """Fit one scene narration into exactly its scene window.

    Speeds the narration up when it is longer than the clip (bounded at 1.3x)
    and pads silence up to the exact scene duration.
    """
    probe = ffprobe_binary()
    if probe is None:
        raise SceneVideoPermanentError(
            "ffprobe is not installed; narration timing is unavailable.",
            code="assembly_probe_missing",
        )
    result = subprocess.run(
        [probe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
         str(source_audio)],
        capture_output=True, timeout=60, check=False,
    )
    try:
        narration_seconds = float(result.stdout.strip() or "0")
    except ValueError:
        narration_seconds = 0.0
    filters: list[str] = []
    if narration_seconds > scene_seconds and narration_seconds > 0:
        tempo = min(1.3, narration_seconds / scene_seconds)
        filters.append(f"atempo={tempo:.4f}")
    filters.append(f"apad=whole_dur={scene_seconds:.3f}")
    return (
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(source_audio),
        "-af", ",".join(filters),
        "-ar", "48000", "-ac", "2",
        "-c:a", "pcm_s16le",
        str(output_path),
    )


def build_audio_concat_args(
    *, ffmpeg: str, concat_list: Path, output_path: Path
) -> tuple[str, ...]:
    """Concatenate the fitted per-scene narrations into one exact-length track."""
    return (
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", str(concat_list),
        "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2",
        str(output_path),
    )


async def run_ffmpeg(command: tuple[str, ...]) -> None:
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
    if process.returncode != 0:
        detail = (stderr or b"").decode("utf-8", "replace").strip()[-400:]
        raise SceneVideoPermanentError(
            f"ffmpeg assembly failed{(f' ({detail})' if detail else '')}.",
            code="assembly_failed",
        )
