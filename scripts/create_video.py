#!/usr/bin/env python
"""Create a Persian talking-avatar MP4 with Piper + Wav2Lip + FFmpeg.

Example:
  python scripts/create_video.py --avatar assets/characters/vandiyar.png \
    --text "سلام، به وندیار خوش آمدید." --duration 10 \
    --output outputs/vandiyar/smoke.mp4 --aspect 9:16
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime"
PIPER = RUNTIME / "piper" / "piper.exe"
VOICE = RUNTIME / "voices" / "fa_IR-amir-medium.onnx"
VOICE_CONFIG = RUNTIME / "voices" / "fa_IR-amir-medium.onnx.json"
FFMPEG = RUNTIME / "ffmpeg" / "bin" / "ffmpeg.exe"
FFPROBE = RUNTIME / "ffmpeg" / "bin" / "ffprobe.exe"
WAV2LIP = RUNTIME / "wav2lip"
AVATAR_PYTHON = RUNTIME / "avatar-venv" / "Scripts" / "python.exe"
CHECKPOINT = WAV2LIP / "checkpoints" / "wav2lip_gan.pth"
FONT_DIR = ROOT / "assets" / "fonts"
ASPECTS = {"9:16": (1080, 1920), "16:9": (1920, 1080), "1:1": (1080, 1080), "4:5": (1080, 1350), "3:4": (1080, 1440)}


class VideoError(RuntimeError):
    pass


def run(command: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None, timeout: int = 1800) -> None:
    print("+ " + subprocess.list2cmdline(command), flush=True)
    try:
        result = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise VideoError(f"Command timed out after {timeout}s: {command[0]}") from exc
    if result.returncode:
        detail = (result.stderr or result.stdout or "No process output")[-5000:]
        raise VideoError(f"Command failed ({result.returncode}): {command[0]}\n{detail}")


def runtime_check() -> list[str]:
    required = {
        "Piper executable": PIPER,
        "Persian Piper voice": VOICE,
        "Piper voice config": VOICE_CONFIG,
        "FFmpeg": FFMPEG,
        "FFprobe": FFPROBE,
        "Wav2Lip inference": WAV2LIP / "inference.py",
        "Wav2Lip checkpoint": CHECKPOINT,
        "S3FD face detector checkpoint": WAV2LIP / "face_detection" / "detection" / "sfd" / "s3fd.pth",
        "Python 3.11 avatar environment": AVATAR_PYTHON,
    }
    missing = [f"{name}: {path}" for name, path in required.items() if not path.is_file() or path.stat().st_size == 0]
    for name, path in required.items():
        print(f"{'OK' if path.is_file() and path.stat().st_size else 'MISSING'} {name}: {path}")
    return missing


def build_ass(text: str, duration: float, width: int, height: int, font_size: int) -> str:
    import re
    cleaned = re.sub(r"[{}]", "", re.sub(r"\s+", " ", text)).strip()
    if not cleaned:
        raise VideoError("Subtitle text is empty.")
    chunks, buffer = [], ""
    for sentence in re.split(r"(?<=[.!?؟،؛:])\s+", cleaned):
        for word in sentence.split():
            candidate = f"{buffer} {word}".strip()
            if len(candidate) > 48 and buffer:
                chunks.append(buffer)
                buffer = word
            else:
                buffer = candidate
    if buffer:
        chunks.append(buffer)
    weights = [max(1, len(item)) for item in chunks]
    total = sum(weights)
    cursor, dialogues = 0.0, []
    for index, chunk in enumerate(chunks):
        end = duration if index == len(chunks) - 1 else cursor + duration * weights[index] / total
        def ts(value: float) -> str:
            h = int(value // 3600)
            m = int((value % 3600) // 60)
            s = value % 60
            return f"{h}:{m:02d}:{s:05.2f}"
        dialogues.append(f"Dialogue: 0,{ts(cursor)},{ts(end)},Persian,,0,0,0,,{chunk}")
        cursor = end
    margin = max(40, int(height * 0.06))
    header = (
        "[Script Info]\n"
        "; Persian subtitles\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {width}\n"
        f"PlayResY: {height}\n"
        "ScaledBorderAndShadow: yes\n"
        "WrapStyle: 0\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Persian,Vazirmatn,{font_size},&H00FFFFFF,&H000000FF,"
        f"&H00101010,&H80000000,0,0,0,0,100,100,0,0,1,3,1,2,60,60,{margin},1\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    return header + "\n".join(dialogues) + "\n"


def probe_duration(media_path: Path) -> float:
    """Return the real media duration reported by ffprobe."""
    try:
        result = subprocess.run(
            [str(FFPROBE), "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(media_path)],
            capture_output=True, text=True, timeout=60,
        )
    except subprocess.TimeoutExpired as exc:
        raise VideoError("Media duration probe timed out.") from exc
    if result.returncode:
        raise VideoError(f"ffprobe could not inspect media: {result.stderr[-2000:]}")
    try:
        duration = float(result.stdout.strip())
    except ValueError as exc:
        raise VideoError("ffprobe returned an invalid media duration.") from exc
    if duration <= 0:
        raise VideoError("Synthesized narration has no positive duration.")
    return duration


def synthesize(text: str, wav_path: Path) -> None:
    command = [str(PIPER), "--model", str(VOICE), "--config", str(VOICE_CONFIG), "--output_file", str(wav_path)]
    print("Stage 1/4: Persian speech synthesis", flush=True)
    try:
        proc = subprocess.run(command, input=text, text=True, capture_output=True, timeout=300)
    except subprocess.TimeoutExpired as exc:
        raise VideoError("Piper speech synthesis timed out.") from exc
    if proc.returncode or not wav_path.is_file() or wav_path.stat().st_size < 1024:
        raise VideoError(f"Piper failed: {(proc.stderr or proc.stdout)[-3000:]}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--avatar", help="A clear, front-facing portrait image.")
    parser.add_argument("--text", help="Persian narration. If omitted, --topic is used as narration.")
    parser.add_argument("--topic", help="Alias/fallback for --text; does not call an LLM.")
    parser.add_argument(
        "--duration", type=int, default=120,
        help="Maximum narration duration; output follows the actual speech length.",
    )
    parser.add_argument("--output", default="outputs/vandiyar/first-video.mp4")
    parser.add_argument("--aspect", choices=tuple(ASPECTS), default="9:16")
    parser.add_argument("--fps", type=int, default=25)
    parser.add_argument("--width", type=int)
    parser.add_argument("--height", type=int)
    parser.add_argument("--font-size", type=int)
    parser.add_argument("--music")
    parser.add_argument("--logo")
    parser.add_argument("--music-volume", type=float, default=0.10)
    parser.add_argument("--keep-work", action="store_true")
    parser.add_argument("--check-runtime", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if args.check_runtime:
        return 1 if runtime_check() else 0
    if not args.avatar:
        parser.error("--avatar is required unless --check-runtime is used")
    missing = runtime_check()
    if missing:
        raise VideoError("Runtime is incomplete. Run scripts/setup_avatar_runtime.py first.")
    avatar = Path(args.avatar)
    if not avatar.is_absolute():
        avatar = ROOT / avatar
    if not avatar.is_file():
        raise VideoError(f"Avatar image not found: {avatar}")
    narration = (args.text or args.topic or "").strip()
    if not narration:
        raise VideoError("Provide Persian narration using --text (or --topic). Topic is not automatically expanded.")
    if not 1 <= args.duration <= 120 or not 1 <= args.fps <= 60:
        raise VideoError("Duration must be 1..120 seconds and FPS 1..60.")
    if not 0 <= args.music_volume <= 1:
        raise VideoError("--music-volume must be between 0 and 1.")
    width, height = ASPECTS[args.aspect]
    width, height = args.width or width, args.height or height
    if width < 160 or height < 160:
        raise VideoError("Output width and height must be at least 160 pixels.")

    output = Path(args.output)
    if not output.is_absolute():
        output = ROOT / output
    output.parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="vandad-avatar-", dir=str(RUNTIME)))
    started = time.perf_counter()
    try:
        wav = work / "narration.wav"
        lipsync = work / "lipsync.mp4"
        ass = work / "subtitles.ass"
        synthesize(narration, wav)
        audio_duration = probe_duration(wav)
        if audio_duration > args.duration:
            raise VideoError(
                f"Speech is {audio_duration:.1f}s, above the configured maximum "
                f"of {args.duration}s. Shorten the narration or raise --duration."
            )
        ass.write_text(
            build_ass(narration, audio_duration, width, height, args.font_size or max(28, height // 40)),
            encoding="utf-8",
        )

        print(f"Stage 2/4: Wav2Lip facial animation ({audio_duration:.1f}s; CPU can be slow)", flush=True)
        env = os.environ.copy()
        env["PATH"] = str(FFMPEG.parent) + os.pathsep + env.get("PATH", "")
        run([
            str(AVATAR_PYTHON), str(WAV2LIP / "inference.py"),
            "--checkpoint_path", str(CHECKPOINT), "--face", str(avatar),
            "--audio", str(wav), "--outfile", str(lipsync), "--fps", str(args.fps),
            "--pads", "0", "10", "0", "0",
        ], cwd=WAV2LIP, env=env, timeout=max(900, args.duration * 120))
        if not lipsync.is_file() or lipsync.stat().st_size < 4096:
            raise VideoError("Wav2Lip did not create a usable video.")

        print("Stage 3/4: FFmpeg composition and Persian subtitles", flush=True)
        ass_arg = str(ass).replace("\\", "/").replace(":", "\\:")
        fonts_arg = str(FONT_DIR).replace("\\", "/").replace(":", "\\:")
        # Preserve the full portrait. A center-crop can cut off the face when
        # the source character image is not already 9:16.
        background = "0A1128"
        vf = (
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=0x{background},"
            f"fps={args.fps},setsar=1,format=yuv420p,"
            f"ass='{ass_arg}':fontsdir='{fonts_arg}'"
        )
        command = [str(FFMPEG), "-y", "-hide_banner", "-loglevel", "error", "-i", str(lipsync)]
        logo = None
        music = None
        if args.logo:
            logo = Path(args.logo)
            if not logo.is_absolute():
                logo = ROOT / logo
            if not logo.is_file():
                raise VideoError(f"Logo not found: {logo}")
            command += ["-i", str(logo)]
        if args.music:
            music = Path(args.music)
            if not music.is_absolute():
                music = ROOT / music
            if not music.is_file():
                raise VideoError(f"Music file not found: {music}")
            command += ["-stream_loop", "-1", "-i", str(music)]

        if logo or music:
            graph = [f"[0:v]{vf}[base]"]
            video_label = "[base]"
            if logo:
                logo_input = 1
                graph.append(f"[{logo_input}:v]scale={max(100, int(width*0.18))}:-1[logo]")
                graph.append("[base][logo]overlay=W-w-36:36[vout]")
                video_label = "[vout]"
            if music:
                music_input = 2 if logo else 1
                graph.append(f"[{music_input}:a]volume={args.music_volume}[music]")
                graph.append("[0:a][music]amix=inputs=2:duration=first:dropout_transition=2[aout]")
            command += ["-filter_complex", ";".join(graph), "-map", video_label]
            if music:
                command += ["-map", "[aout]"]
            else:
                command += ["-map", "0:a?"]
        else:
            command += ["-vf", vf, "-map", "0:v:0", "-map", "0:a?"]
        command += [
            "-shortest", "-c:v", "libx264", "-preset", "veryfast",
            "-crf", "21", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
            "-movflags", "+faststart", str(output),
        ]
        run(command, timeout=max(300, args.duration * 60))

        print("Stage 4/4: MP4 verification", flush=True)
        probe = subprocess.run(
            [str(FFPROBE), "-v", "error", "-show_entries", "format=duration:stream=codec_type,width,height",
             "-of", "json", str(output)],
            capture_output=True, text=True, timeout=60,
        )
        if probe.returncode:
            raise VideoError(f"ffprobe failed: {probe.stderr[-2000:]}")
        metadata = json.loads(probe.stdout)
        duration = float(metadata.get("format", {}).get("duration", 0))
        streams = metadata.get("streams", [])
        has_video = any(stream.get("codec_type") == "video" for stream in streams)
        has_audio = any(stream.get("codec_type") == "audio" for stream in streams)
        if not has_video or not has_audio or duration <= 0:
            raise VideoError("Output MP4 must contain valid video and audio streams.")
        if abs(duration - audio_duration) > 1.0:
            raise VideoError(
                f"Output duration ({duration:.2f}s) does not match narration "
                f"({audio_duration:.2f}s)."
            )
        video_stream = next(stream for stream in streams if stream.get("codec_type") == "video")
        if video_stream.get("width") != width or video_stream.get("height") != height:
            raise VideoError("Output MP4 resolution does not match the requested canvas.")
        report = {
            "status": "completed", "output": str(output), "duration_seconds": round(duration, 2),
            "resolution": f"{width}x{height}", "fps": args.fps,
            "elapsed_seconds": round(time.perf_counter() - started, 2),
        }
        print(json.dumps(report, ensure_ascii=False) if args.json else f"Created: {output}\nDuration: {duration:.2f}s")
        return 0
    finally:
        if args.keep_work:
            print(f"Work files retained: {work}")
        else:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except VideoError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
