"""Local FFmpeg scene-clip assembly.

Multi-scene projects whose provider cannot compose a final storyboard video
are assembled locally: the validated scene clips are concatenated into the
final vertical deliverable. Audio-capable providers keep their native
soundtrack; real platform screenshots are overlaid during their scene window
so the deliverable shows the actual product UI. No subtitle or caption is ever
burned in. This is deterministic media stitching — no creative generation.
"""

from __future__ import annotations

import asyncio
import json
import math
import shutil
import subprocess
from pathlib import Path
from typing import Any

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


def probe_video_streams(path: Path) -> dict[str, Any] | None:
    """Return container/stream facts for a finished video.

    Returns ``None`` when ffprobe is not installed so callers can decide
    whether probing is mandatory. Unreadable or malformed media raises
    :class:`SceneVideoPermanentError` instead of producing partial facts.
    """
    probe = ffprobe_binary()
    if probe is None:
        return None
    result = subprocess.run(
        [probe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        capture_output=True, timeout=120, check=False,
    )
    if result.returncode != 0:
        raise SceneVideoPermanentError(
            "The finished video could not be probed; the file may be corrupt.",
            code="invalid_video",
        )
    try:
        payload = json.loads(result.stdout or b"{}")
    except ValueError as exc:
        raise SceneVideoPermanentError(
            "The finished video returned unreadable probe metadata.",
            code="invalid_video",
        ) from exc
    if not isinstance(payload, dict):
        raise SceneVideoPermanentError(
            "The finished video returned unreadable probe metadata.",
            code="invalid_video",
        )
    streams = [
        stream for stream in (payload.get("streams") or [])
        if isinstance(stream, dict)
    ]
    video_stream = next(
        (stream for stream in streams if stream.get("codec_type") == "video"), None
    )
    audio_streams = [
        stream for stream in streams if stream.get("codec_type") == "audio"
    ]
    format_info = payload.get("format") if isinstance(payload.get("format"), dict) else {}
    duration = _probe_float(format_info.get("duration"))
    if duration is None:
        durations = [
            value
            for value in (
                _probe_float(stream.get("duration"))
                for stream in [video_stream, *audio_streams]
                if isinstance(stream, dict)
            )
            if value is not None
        ]
        duration = max(durations) if durations else None
    return {
        "has_video": video_stream is not None,
        "has_audio": bool(audio_streams),
        "width": int(_probe_float(video_stream.get("width")) or 0) if video_stream else 0,
        "height": int(_probe_float(video_stream.get("height")) or 0) if video_stream else 0,
        "video_codec": (video_stream or {}).get("codec_name"),
        "audio_codec": (audio_streams[0].get("codec_name") if audio_streams else None),
        "duration_seconds": duration,
        "audio_stream_count": len(audio_streams),
    }


def _probe_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def clip_duration_seconds(path: Path) -> float | None:
    """Return a clip's container duration, or None when it cannot be probed."""
    probe = ffprobe_binary()
    if probe is None:
        return None
    result = subprocess.run(
        [probe, "-v", "error", "-show_entries", "format=duration", "-of",
         "csv=p=0", str(path)],
        capture_output=True, timeout=60, check=False,
    )
    return _probe_float(result.stdout.strip())


def build_xfade_concat_args(
    *,
    ffmpeg: str,
    clip_paths: tuple[Path, ...],
    output_path: Path,
    width: int,
    height: int,
    scene_seconds: tuple[float, ...],
    transitions: tuple[str, ...],
    with_audio: bool,
) -> tuple[str, ...]:
    """Build a cinematic multi-scene concat with motivated dissolve transitions.

    A ``cut`` is a near-instant handoff; other values map to deterministic
    FFmpeg xfade/acrossfade transitions. The chain is padded back to the exact
    summed scene length, so transitions never shorten the deliverable.
    """
    if len(clip_paths) < 2 or len(scene_seconds) != len(clip_paths):
        raise SceneVideoPermanentError(
            "Cinematic transitions require one duration per scene and at least two scenes.",
            code="assembly_transition_input_invalid",
        )
    if len(transitions) != len(clip_paths) - 1:
        raise SceneVideoPermanentError(
            "Cinematic transitions require one transition between every adjacent pair.",
            code="assembly_transition_input_invalid",
        )

    kind_map = {
        "cut": "fade", "dissolve": "fade", "fade": "fade",
        "whip": "slide", "match_cut": "fade",
    }
    durations = []
    for value in scene_seconds:
        if value <= 0 or not math.isfinite(value):
            raise SceneVideoPermanentError(
                "Cinematic transitions require finite positive scene durations.",
                code="assembly_transition_input_invalid",
            )
        durations.append(value)
    transitions_seconds = tuple(
        max(0.04, min(0.5, value * 0.08)) for value in durations[:-1]
    )
    command: list[str] = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error"]
    for path in clip_paths:
        command += ["-i", str(path)]

    filters: list[str] = []
    for index in range(len(clip_paths)):
        filters.append(
            f"[{index}:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,fps=30,format=yuv420p,"
            f"setsar=1[v{index}]"
        )
    video_chain = "v0"
    offset = 0.0
    for index, name in enumerate(transitions, start=1):
        offset += durations[index - 1] - transitions_seconds[index - 1]
        kind = kind_map.get(str(name).strip().lower(), "fade")
        output = f"vx{index}"
        filters.append(
            f"[{video_chain}][v{index}]xfade=transition={kind}:"
            f"duration={transitions_seconds[index - 1]:.3f}:"
            f"offset={max(0.0, offset):.3f}[{output}]"
        )
        video_chain = output
    total = sum(durations)
    filters.append(
        f"[{video_chain}]tpad=stop_mode=clone:stop_duration="
        f"{sum(transitions_seconds):.3f}[vout]"
    )

    audio_chain: str | None = None
    if with_audio:
        for index in range(len(clip_paths)):
            filters.append(
                f"[{index}:a]aresample=48000,aformat=channel_layouts=stereo[a{index}]"
            )
        audio_chain = "a0"
        for index in range(1, len(clip_paths)):
            output = f"ax{index}"
            filters.append(
                f"[{audio_chain}][a{index}]acrossfade=d={transitions_seconds[index - 1]:.3f}"
                f":c1=tri:c2=tri[{output}]"
            )
            audio_chain = output
        filters.append(f"[{audio_chain}]apad=whole_dur={total:.3f}[aout]")

    command += [
        "-filter_complex", ";".join(filters),
        "-map", "[vout]",
        *([ "-map", "[aout]", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2" ]
          if audio_chain else ["-an"]),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-pix_fmt", "yuv420p", "-t", f"{total:.3f}", "-movflags", "+faststart",
        str(output_path),
    ]
    return tuple(command)


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
    with_audio: bool,
) -> tuple[str, ...] | None:
    """Build the stage-2 command for deterministic screenshot overlays.

    Subtitles are deliberately not supported: the provider audio is already
    synchronized and burned-in captions are unreliable. Returns None when there
    are no overlays to apply; the caller then keeps stage-1 output as final.
    """
    if not overlays:
        return None
    overlay_scale_width = int(width * _MAX_OVERLAY_WIDTH_RATIO)
    overlay_scale_height = int(height * _MAX_OVERLAY_HEIGHT_RATIO)
    filters: list[str] = []
    chain = "[0:v]"
    for index, (_image, start, end) in enumerate(overlays):
        scaled = f"ov{index}"
        filters.append(
            f"[{index + 1}:v]scale={overlay_scale_width}:{overlay_scale_height}:"
            f"force_original_aspect_ratio=decrease[{scaled}]"
        )
        next_chain = f"ovx{index}"
        filters.append(
            f"{chain}[{scaled}]overlay=(W-w)/2:(H-h)/2:"
            f"enable='between(t,{start:.3f},{end:.3f})'[{next_chain}]"
        )
        chain = f"[{next_chain}]"
    command: list[str] = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(source_path),
    ]
    for image, _start, _end in overlays:
        command += ["-loop", "1", "-i", str(image)]
    command += [
        "-filter_complex", ";".join(filters),
        "-map", chain,
        # The looped image inputs are infinite streams; -shortest ends the
        # encode at the source video's duration.
        "-shortest",
        *(["-map", "0:a?", "-c:a", "aac", "-b:a", "192k"] if with_audio else ["-an"]),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
    ]
    command.append(str(output_path))
    return tuple(command)


async def concat_scene_clips(
    clip_paths: tuple[Path, ...],
    *,
    output_path: Path,
    width: int,
    height: int,
    overlays: tuple[tuple[Path, float, float], ...] = (),
    transitions: tuple[str, ...] = (),
    scene_seconds: tuple[float, ...] = (),
    music_path: Path | None = None,
    music_gain: float = 0.12,
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
    use_xfade = bool(transitions and len(clip_paths) > 1)
    durations = tuple(float(value) for value in scene_seconds)
    if use_xfade and len(durations) != len(clip_paths):
        probed = tuple(clip_duration_seconds(path) for path in clip_paths)
        durations = tuple(value for value in probed if value is not None)
        if len(durations) != len(clip_paths):
            # Without reliable timings, fall back to the exact plain concat
            # rather than guessing xfade offsets.
            use_xfade = False
            durations = ()
    total_seconds = sum(durations) if durations else 0.0

    output_path.parent.mkdir(parents=True, exist_ok=True)
    concat_list = output_path.with_suffix(".concat.txt")
    concat_list.write_text(
        "".join(f"file '{path.as_posix()}'\n" for path in clip_paths),
        encoding="utf-8",
    )
    temp_full = output_path.with_suffix(".full.mp4")
    mixed = output_path.with_suffix(".mixed.mp4")
    try:
        if use_xfade:
            await run_ffmpeg(
                build_xfade_concat_args(
                    ffmpeg=ffmpeg, clip_paths=tuple(clip_paths), output_path=temp_full,
                    width=width, height=height, scene_seconds=durations,
                    transitions=tuple(transitions), with_audio=with_audio,
                ),
            )
        else:
            await run_ffmpeg(
                build_concat_args(
                    ffmpeg=ffmpeg, concat_list=concat_list, output_path=temp_full,
                    width=width, height=height, with_audio=with_audio,
                ),
            )
        finish_args = build_finish_args(
            ffmpeg=ffmpeg, source_path=temp_full, output_path=output_path,
            width=width, height=height, overlays=overlays, with_audio=with_audio,
        )
        if finish_args is None:
            temp_full.replace(output_path)
        else:
            await run_ffmpeg(finish_args)
        if music_path is not None and with_audio and total_seconds > 0:
            await run_ffmpeg(
                build_music_mix_args(
                    ffmpeg=ffmpeg, video_path=output_path, music_path=music_path,
                    output_path=mixed, total_seconds=total_seconds, music_gain=music_gain,
                ),
            )
            mixed.replace(output_path)
    finally:
        concat_list.unlink(missing_ok=True)
        temp_full.unlink(missing_ok=True)
        mixed.unlink(missing_ok=True)

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


def build_endcard_video_args(
    *,
    ffmpeg: str,
    image_path: Path,
    output_path: Path,
    seconds: float,
    width: int,
    height: int,
) -> tuple[str, ...]:
    """Turn the end-card PNG into a fixed-length silent-audio video segment.

    The segment always carries a silent stereo track so concat never hits a
    mixed audio/no-audio stream set.
    """
    return (
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-loop", "1", "-i", str(image_path),
        "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
        "-t", f"{seconds:.3f}",
        "-vf", (
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,fps=30,format=yuv420p"
        ),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
        "-shortest", "-movflags", "+faststart",
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


def build_music_mix_args(
    *,
    ffmpeg: str,
    video_path: Path,
    music_path: Path,
    output_path: Path,
    total_seconds: float,
    music_gain: float = 0.12,
) -> tuple[str, ...]:
    """Mix an optional licensed bed under the existing native narration audio.

    Speech remains the primary source (gain 1.0); the bed is ducked to
    ``music_gain`` with equal-power cross fade at both ends and a limiter keeps
    the final programme level bounded. No music is generated or downloaded.
    """
    if not music_path.is_file():
        raise SceneVideoPermanentError(
            "The supplied music bed does not exist on disk.", code="assembly_music_missing"
        )
    gain = max(0.0, min(0.35, float(music_gain)))
    fade = max(0.2, min(2.0, total_seconds / 10.0))
    fade_start = max(0.0, total_seconds - fade)
    filters = (
        f"[1:a]aresample=48000,aformat=channel_layouts=stereo,"
        f"volume={gain:.3f},afade=t=in:st=0:d={fade:.3f},"
        f"afade=t=out:st={fade_start:.3f}:d={fade:.3f}[bed];"
        f"[0:a][bed]amix=inputs=2:duration=first:dropout_transition=0,"
        f"alimiter=limit=0.95[aout]"
    )
    return (
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(video_path), "-stream_loop", "-1", "-i", str(music_path),
        "-filter_complex", filters,
        "-map", "0:v:0", "-map", "[aout]",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
        "-t", f"{total_seconds:.3f}", "-movflags", "+faststart",
        str(output_path),
    )


def qc_video_file(
    path: Path,
    *,
    expected_duration_seconds: float,
    expected_width: int,
    expected_height: int,
    expect_audio: bool,
    duration_tolerance_seconds: float = 1.0,
) -> dict[str, Any]:
    """Deterministic structural QC for one assembled deliverable.

    This verifies container facts only. Pronunciation, acting quality and brand
    correctness still require a human listening/viewing pass; the report says so
    explicitly so nobody mistakes a passing probe for creative acceptance.
    """
    probe = probe_video_streams(path)
    if probe is None:
        return {
            "status": "skipped",
            "reason": "ffprobe is not installed; structural QC is unavailable.",
            "requires_human_review": True,
            "checks": [],
        }

    checks: list[dict[str, Any]] = []

    def check(name: str, passed: bool, detail: str) -> None:
        checks.append({"name": name, "passed": bool(passed), "detail": detail})

    check("has_video", bool(probe["has_video"]), "video stream present")
    width_ok = int(probe["width"] or 0) == int(expected_width)
    height_ok = int(probe["height"] or 0) == int(expected_height)
    check(
        "resolution",
        width_ok and height_ok,
        f"{probe['width']}x{probe['height']} (expected {expected_width}x{expected_height})",
    )
    ratio = (
        float(probe["width"]) / float(probe["height"])
        if probe["height"] else 0.0
    )
    check(
        "vertical_aspect",
        abs(ratio - 9.0 / 16.0) <= 0.02,
        f"aspect={ratio:.4f} (expected 9:16)",
    )
    duration = _probe_float(probe["duration_seconds"])
    duration_ok = (
        duration is not None
        and abs(duration - float(expected_duration_seconds)) <= float(duration_tolerance_seconds)
    )
    check(
        "duration",
        bool(duration_ok),
        f"{duration if duration is not None else 'unknown'}s "
        f"(expected {float(expected_duration_seconds):.3f}s "
        f"+/-{float(duration_tolerance_seconds):.3f}s)",
    )
    if expect_audio:
        check(
            "native_audio",
            bool(probe["has_audio"]),
            f"audio_streams={probe['audio_stream_count']}, codec={probe['audio_codec']}",
        )
    else:
        check("no_audio_expected", not probe["has_audio"], "audio stream absent as expected")

    return {
        "status": "passed" if all(item["passed"] for item in checks) else "failed",
        "probe": probe,
        "checks": checks,
        "requires_human_review": True,
        "note": "Structural probe only; pronunciation and creative quality require listening/viewing.",
    }


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
