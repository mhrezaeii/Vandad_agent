#!/usr/bin/env python
"""Install the pinned, local Windows talking-avatar runtime.

The setup is idempotent. Normal rendering never downloads anything.
Requires Python 3.11 for the isolated Wav2Lip environment.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime"
PIPER_DIR, VOICE_DIR = RUNTIME / "piper", RUNTIME / "voices"
FFMPEG_DIR, WAV2LIP_DIR = RUNTIME / "ffmpeg", RUNTIME / "wav2lip"
VENV_DIR, FONT_DIR = RUNTIME / "avatar-venv", ROOT / "assets" / "fonts"
PIPER_URL = "https://github.com/rhasspy/piper/releases/download/2023.11.14-2/piper_windows_amd64.zip"
VOICE_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/main/fa/fa_IR/amir/medium"
VOICE_NAME = "fa_IR-amir-medium"
FFMPEG_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
FONT_URL = "https://github.com/rastikerdar/vazirmatn/releases/download/v33.003/vazirmatn-v33.003.zip"
WAV2LIP_REF = "bac9a81e63ecc153202353372e5724b83d9e6322"
WAV2LIP_URL = f"https://github.com/Rudrabha/Wav2Lip/archive/{WAV2LIP_REF}.zip"
WAV2LIP_WEIGHTS = "https://huggingface.co/camenduru/Wav2Lip/resolve/main/checkpoints/wav2lip_gan.pth"
S3FD_WEIGHTS = "https://huggingface.co/camenduru/Wav2Lip/resolve/main/checkpoints/s3fd-619a316812.pth"


def download(url: str, target: Path, force: bool = False) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file() and target.stat().st_size and not force:
        print(f"  reuse {target.relative_to(ROOT)}")
        return target
    temp = target.with_name(target.name + ".part")
    temp.unlink(missing_ok=True)
    print(f"  download {url}")
    try:
        with urllib.request.urlopen(url, timeout=180) as response, temp.open("wb") as out:
            while chunk := response.read(1024 * 1024):
                out.write(chunk)
        if not temp.is_file() or not temp.stat().st_size:
            raise RuntimeError(f"Empty download: {url}")
        temp.replace(target)
    except Exception:
        temp.unlink(missing_ok=True)
        raise
    return target


def safe_extract(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    with zipfile.ZipFile(archive) as zf:
        for member in zf.infolist():
            target = (destination / member.filename).resolve()
            if target != root and root not in target.parents:
                raise RuntimeError(f"Unsafe ZIP member path: {member.filename}")
        zf.extractall(destination)


def move_single_root(source: Path, destination: Path) -> None:
    roots = [item for item in source.iterdir() if item.is_dir()]
    if len(roots) != 1:
        raise RuntimeError(f"Unexpected archive layout: {source}")
    shutil.rmtree(destination, ignore_errors=True)
    shutil.move(str(roots[0]), str(destination))


def setup_piper(force: bool) -> None:
    print("[piper]")
    if not (PIPER_DIR / "piper.exe").is_file() or force:
        archive = download(PIPER_URL, RUNTIME / "piper.zip", force)
        temp = RUNTIME / "_piper_extract"
        shutil.rmtree(temp, ignore_errors=True)
        safe_extract(archive, temp)
        candidates = list(temp.rglob("piper.exe"))
        if not candidates:
            raise RuntimeError("piper.exe not found in archive")
        PIPER_DIR.mkdir(parents=True, exist_ok=True)
        for item in candidates[0].parent.iterdir():
            target = PIPER_DIR / item.name
            if item.is_dir():
                shutil.copytree(item, target, dirs_exist_ok=True)
            else:
                shutil.copy2(item, target)
        shutil.rmtree(temp, ignore_errors=True)
    download(f"{VOICE_BASE}/{VOICE_NAME}.onnx", VOICE_DIR / f"{VOICE_NAME}.onnx", force)
    download(f"{VOICE_BASE}/{VOICE_NAME}.onnx.json", VOICE_DIR / f"{VOICE_NAME}.onnx.json", force)


def setup_ffmpeg(force: bool) -> None:
    print("[ffmpeg]")
    if (FFMPEG_DIR / "bin" / "ffmpeg.exe").is_file() and not force:
        print("  ffmpeg already present")
        return
    archive = download(FFMPEG_URL, RUNTIME / "ffmpeg.zip", force)
    temp = RUNTIME / "_ffmpeg_extract"
    shutil.rmtree(temp, ignore_errors=True)
    safe_extract(archive, temp)
    move_single_root(temp, FFMPEG_DIR)
    shutil.rmtree(temp, ignore_errors=True)


def setup_fonts(force: bool) -> None:
    print("[fonts]")
    FONT_DIR.mkdir(parents=True, exist_ok=True)
    names = ("Vazirmatn-Regular.ttf", "Vazirmatn-Bold.ttf")
    if all((FONT_DIR / name).is_file() for name in names) and not force:
        print("  Vazirmatn already present")
        return
    archive = download(FONT_URL, RUNTIME / "vazirmatn.zip", force)
    temp = RUNTIME / "_font_extract"
    shutil.rmtree(temp, ignore_errors=True)
    safe_extract(archive, temp)
    for name in (*names, "Vazirmatn-Medium.ttf"):
        matches = list(temp.rglob(name))
        if matches:
            shutil.copy2(matches[0], FONT_DIR / name)
    shutil.rmtree(temp, ignore_errors=True)
    if not all((FONT_DIR / name).is_file() for name in names):
        raise RuntimeError("Vazirmatn regular/bold font files are missing")


def patch_wav2lip() -> None:
    """Patch Windows subprocess calls; never rewrite torch.load source."""
    inference = WAV2LIP_DIR / "inference.py"
    text = inference.read_text(encoding="utf-8")
    text = text.replace(
        "args.face.split('.')[1] in ['jpg', 'png', 'jpeg']",
        "os.path.splitext(args.face)[1].lower() in ['.jpg', '.png', '.jpeg']",
    )
    text = text.replace(
        "\t\tcommand = 'ffmpeg -y -i {} -strict -2 {}'.format(args.audio, 'temp/temp.wav')\n"
        "\t\tsubprocess.call(command, shell=True)",
        "\t\tcommand = ['ffmpeg', '-y', '-i', args.audio, '-strict', '-2', 'temp/temp.wav']\n"
        "\t\tsubprocess.run(command, check=True)",
    )
    text = text.replace(
        "\tcommand = 'ffmpeg -y -i {} -i {} -strict -2 -q:v 1 {}'.format(args.audio, 'temp/result.avi', args.outfile)\n"
        "\tsubprocess.call(command, shell=platform.system() != 'Windows')",
        "\tcommand = ['ffmpeg', '-y', '-i', args.audio, '-i', 'temp/result.avi', "
        "'-strict', '-2', '-q:v', '1', args.outfile]\n"
        "\tsubprocess.run(command, check=True)",
    )
    inference.write_text(text, encoding="utf-8")
    for name in ("temp", "results"):
        (WAV2LIP_DIR / name).mkdir(parents=True, exist_ok=True)


def setup_wav2lip(force: bool) -> None:
    print("[wav2lip]")
    if not (WAV2LIP_DIR / "inference.py").is_file() or force:
        archive = download(WAV2LIP_URL, RUNTIME / "wav2lip.zip", force)
        temp = RUNTIME / "_wav2lip_extract"
        shutil.rmtree(temp, ignore_errors=True)
        safe_extract(archive, temp)
        move_single_root(temp, WAV2LIP_DIR)
        shutil.rmtree(temp, ignore_errors=True)
    checkpoints = WAV2LIP_DIR / "checkpoints"
    checkpoints.mkdir(parents=True, exist_ok=True)
    download(WAV2LIP_WEIGHTS, checkpoints / "wav2lip_gan.pth", force)
    s3fd = download(S3FD_WEIGHTS, checkpoints / "s3fd-619a316812.pth", force)
    detector = WAV2LIP_DIR / "face_detection" / "detection" / "sfd"
    detector.mkdir(parents=True, exist_ok=True)
    shutil.copy2(s3fd, detector / "s3fd.pth")
    patch_wav2lip()


def find_python311() -> str:
    configured = os.environ.get("AVATAR_PYTHON")
    candidates = [configured] if configured else []
    candidates.extend(["E:/python/python.exe", shutil.which("python3.11") or "", shutil.which("py") or ""])
    for candidate in candidates:
        if not candidate:
            continue
        command = [candidate, "-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"]
        if Path(candidate).stem.lower() == "py":
            command = [
                candidate, "-3.11", "-c",
                "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')",
            ]
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode == 0 and result.stdout.strip() == "3.11":
            return candidate
    raise RuntimeError("Python 3.11 not found; set AVATAR_PYTHON to its executable")


def setup_venv(force: bool) -> None:
    print("[avatar venv]")
    python = find_python311()
    if force:
        shutil.rmtree(VENV_DIR, ignore_errors=True)
    if not (VENV_DIR / "Scripts" / "python.exe").is_file():
        subprocess.run([python, "-m", "venv", str(VENV_DIR)], check=True)
    vpy = VENV_DIR / "Scripts" / "python.exe"
    subprocess.run([str(vpy), "-m", "pip", "install", "--upgrade", "pip", "wheel"], check=True)
    subprocess.run([
        str(vpy), "-m", "pip", "install", "torch==2.5.1", "torchvision==0.20.1",
        "--index-url", "https://download.pytorch.org/whl/cpu",
    ], check=True)
    subprocess.run([
        str(vpy), "-m", "pip", "install", "numpy==1.26.4", "scipy==1.11.4",
        "opencv-python-headless==4.10.0.84", "librosa==0.10.2.post1",
        "numba==0.60.0", "soundfile==0.12.1", "Pillow==10.4.0", "tqdm==4.66.5",
    ], check=True)
    subprocess.run([
        str(vpy), "-c",
        "import torch, cv2, librosa, numpy, scipy, soundfile; "
        "assert torch.__version__.startswith('2.5.1'); print('imports OK', torch.__version__)",
    ], check=True)


def verify(require_venv: bool) -> int:
    print("\n[verify]")
    required = {
        "Piper": PIPER_DIR / "piper.exe",
        "Persian voice": VOICE_DIR / f"{VOICE_NAME}.onnx",
        "Voice config": VOICE_DIR / f"{VOICE_NAME}.onnx.json",
        "FFmpeg": FFMPEG_DIR / "bin" / "ffmpeg.exe",
        "FFprobe": FFMPEG_DIR / "bin" / "ffprobe.exe",
        "Wav2Lip": WAV2LIP_DIR / "inference.py",
        "Wav2Lip checkpoint": WAV2LIP_DIR / "checkpoints" / "wav2lip_gan.pth",
        "S3FD checkpoint": WAV2LIP_DIR / "face_detection" / "detection" / "sfd" / "s3fd.pth",
        "Vazirmatn regular": FONT_DIR / "Vazirmatn-Regular.ttf",
        "Vazirmatn bold": FONT_DIR / "Vazirmatn-Bold.ttf",
    }
    if require_venv:
        required["Python 3.11 venv"] = VENV_DIR / "Scripts" / "python.exe"
    missing = []
    for label, path in required.items():
        ok = path.is_file() and path.stat().st_size > 0
        print(f"  {'OK' if ok else 'MISSING'} {label}: {path}")
        if not ok:
            missing.append(label)
    if missing:
        print("Setup incomplete: " + ", ".join(missing))
        return 1
    print("Runtime ready.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--skip-venv", action="store_true")
    args = parser.parse_args()
    RUNTIME.mkdir(parents=True, exist_ok=True)
    setup_piper(args.force)
    setup_ffmpeg(args.force)
    setup_fonts(args.force)
    setup_wav2lip(args.force)
    if not args.skip_venv:
        setup_venv(args.force)
    return verify(require_venv=not args.skip_venv)


if __name__ == "__main__":
    raise SystemExit(main())
