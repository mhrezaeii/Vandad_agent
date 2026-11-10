"""Fast, hardware-independent tests for the local talking-avatar scripts."""
from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_script():
    path = ROOT / "scripts" / "create_video.py"
    spec = importlib.util.spec_from_file_location("vandad_create_video", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_avatar_scripts_are_valid_python():
    for relative in ("scripts/create_video.py", "scripts/setup_avatar_runtime.py"):
        ast.parse((ROOT / relative).read_text(encoding="utf-8"))


def test_ass_subtitles_have_persian_font_and_timing():
    module = _load_script()
    result = module.build_ass("سلام به وندیار. این اولین ویدیوی ماست.", 10, 1080, 1920, 48)
    assert "Vazirmatn" in result
    assert "PlayResX: 1080" in result
    assert "PlayResY: 1920" in result
    assert "سلام به وندیار" in result
    assert "Dialogue: 0,0:00:00.00" in result


def test_ass_subtitles_reject_blank_narration():
    module = _load_script()
    try:
        module.build_ass("   ", 10, 1080, 1920, 48)
    except module.VideoError:
        return
    raise AssertionError("Blank narration must be rejected.")


def test_ass_subtitles_use_actual_speech_duration():
    module = _load_script()
    result = module.build_ass("سلام به وندیار. این اولین ویدیوی ماست.", 7.5, 1080, 1920, 48)
    assert "0:00:07.50" in result
