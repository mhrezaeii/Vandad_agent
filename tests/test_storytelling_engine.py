"""Cinematic storytelling engine regression tests (no network, no paid calls)."""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from app.schemas.scenario import ScenarioRequest, normalize_scenario
from app.schemas.storytelling import (
    ACT_DURATION_SHARE,
    CharacterBible,
    CharacterSpec,
    Storyboard,
    allocate_act_durations,
)
from app.services import video_assembly
from app.services.branding import display_text, fit_text_block, render_end_card, wrap_text
from app.services.prompt_composer import PROMPT_SECTIONS, compose_single_shot_prompt
from app.services.scenario_review import review_scenario
from app.services.scene_video_service import build_scene_provider_prompt
from app.services.storytelling import build_storyboard, character_block, storyboard_issues

_DIACRITICS = "ًٌٍَُِّْٰ"


def _vowelize(text: str) -> str:
    def mark(word: str) -> str:
        plain = "".join(char for char in word if char not in _DIACRITICS)
        if len(plain) < 4:
            return word
        return plain[0] + "َ" + plain[1:]

    return " ".join(mark(word) for word in text.split())


def _raw_cinematic_scenario() -> dict:
    texts = [
        "وقتی کارها پراکنده میشوند، دید کلی دشوار میشود و تیم سردرگم میماند.",
        "هر پیام تازه، مشکلات تازهای را در یک روز شلوغ به وجود میآورد.",
        "وندیار با نمای روشن و ساده، همه اطلاعات را در یک جای واحد گرد میآورد.",
        "تیم با دید تازه، کارها را سریعتر هماهنگ میکند و پیشرفت را میبیند.",
        "از همین امروز با وندیار آشنا شوید و مسیر کار خود را سادهتر کنید.",
    ]
    scenes = []
    for index, text in enumerate(texts):
        narration = _vowelize(text)
        scenes.append(
            {
                "index": index,
                "scene_id": f"scene-{index + 1:02d}",
                "narrative_purpose": [
                    "ایجاد همدلی با مشکل",
                    "تشدید چالش",
                    "معرفی راه حل",
                    "نشان دادن تحول",
                    "پایان برند و دعوت به اقدام",
                ][index],
                "scene_type": (
                    "problem" if index == 0
                    else "product_reveal" if index == 2
                    else "transformation" if index == 3
                    else "product_demo"
                ),
                "narration": narration,
                "pronunciation_text": narration,
                "visual_prompt": f"Distinct cinematic 3D scene {index + 1} with clear framing.",
                "duration_seconds": 8,
                "on_screen_text": None,
                "characters": ["maya"],
                "character_actions": f"Character action {index + 1} moves through the scene.",
                "camera_direction": f"Cinematic camera move {index + 1}",
                "transition": "dissolve",
            }
        )
    cta = _vowelize("با وندیار شروع کنید")
    return {
        "title": _vowelize("معرفی وندیار"),
        "opening_hook": _vowelize("یک قلاب کوتاه و روشن"),
        "visual_style_preset": "cinematic_3d_storytelling",
        "character_bible": {
            "style_preset": "cinematic_3d_storytelling",
            "characters": [
                {
                    "character_id": "maya",
                    "name": "Maya",
                    "age_range": "30s",
                    "face_shape": "soft oval face",
                    "hair_color": "dark brown hair",
                    "clothing": "navy blazer",
                    "personality": "calm and optimistic",
                }
            ],
            "continuity_rules": ["Keep the same face, hair and navy blazer in every scene."],
        },
        "scenes": scenes,
        "cta": cta,
        "cta_pronunciation_text": cta,
        "caption": _vowelize("کپشن کوتاه"),
        "hashtags": ["#وندیار"],
        "target_duration_seconds": 40,
    }


def test_act_allocation_covers_all_five_acts_and_sums_exactly():
    result = allocate_act_durations(40, 5)
    assert [act for act, _ in result] == list(ACT_DURATION_SHARE)
    assert sum(seconds for _, seconds in result) == pytest.approx(40, abs=0.01)
    transformation = dict(result)["transformation"]
    escalation = dict(result)["escalation"]
    assert transformation > escalation * 2


def test_character_bible_locks_identity_and_rejects_duplicate_identities():
    bible = CharacterBible(
        characters=[
            CharacterSpec(character_id="maya", name="Maya", clothing="navy blazer"),
            CharacterSpec(
                character_id="robot", name="Ava", is_robot=True,
                robot_design_lock="small friendly white robot with round blue eyes",
            ),
        ]
    )
    block = character_block(bible)
    assert "CHARACTER BIBLE" in block
    assert "navy blazer" in block
    assert "small friendly white robot" in block
    with pytest.raises(ValueError, match="unique"):
        CharacterBible(
            characters=[
                CharacterSpec(character_id="maya", name="Maya"),
                CharacterSpec(character_id="maya", name="Maya"),
            ]
        )


def test_storyboard_is_built_from_scenario_and_qc_detects_broken_continuity():
    scenario = normalize_scenario(
        _raw_cinematic_scenario(), ScenarioRequest(target_duration_seconds=40)
    )
    storyboard = build_storyboard(scenario)
    assert storyboard.style_preset == "cinematic_3d_storytelling"
    assert [scene.act for scene in storyboard.scenes] == list(ACT_DURATION_SHARE)
    assert storyboard_issues(storyboard) == []

    broken = Storyboard.model_validate(storyboard.model_dump())
    broken.scenes[1].characters = ["unknown"]
    issues = storyboard_issues(broken)
    assert any("unknown character" in issue for issue in issues)


def test_review_scenario_runs_storyboard_gate_on_normalized_scenario():
    scenario = normalize_scenario(
        _raw_cinematic_scenario(), ScenarioRequest(target_duration_seconds=40)
    )
    assert review_scenario(scenario, target_duration_seconds=40) == []

    raw = _raw_cinematic_scenario()
    raw["scenes"][1]["narrative_purpose"] = raw["scenes"][0]["narrative_purpose"]
    degraded = normalize_scenario(raw, ScenarioRequest(target_duration_seconds=40))
    issues = review_scenario(degraded, target_duration_seconds=40)
    assert any("storyboard" in issue for issue in issues)


def test_prompt_composer_has_ten_stable_sections_and_uses_pronunciation_text():
    scenario = normalize_scenario(
        _raw_cinematic_scenario(), ScenarioRequest(target_duration_seconds=40)
    )
    prompt = compose_single_shot_prompt(scenario, max_characters=12000)
    assert len(PROMPT_SECTIONS) == 10
    headings = [
        "1) GOAL & CONTINUITY", "2) IDENTITY & CHARACTER BIBLE",
        "3) VISUAL WORLD", "4) EMOTIONAL STORY ARC", "5) TIMING & PACING",
        "6) CINEMATOGRAPHY", "7) SPEECH & PRONUNCIATION", "8) SOUND DESIGN",
        "9) TEXT-FREE FRAME & BRAND END", "10) STORYBOARD BEATS & CTA",
    ]
    for heading in headings:
        assert heading in prompt
    assert "STORYBOARD (play beats in this exact order):" in prompt
    assert "PRONUNCIATION" in prompt
    assert "CHARACTER BIBLE" in prompt
    assert "NO ON-SCREEN TEXT" in prompt
    for scene in scenario.scenes:
        assert scene.pronunciation_text in prompt
        assert prompt.count(scene.narration) == 1


def test_scene_provider_prompt_uses_pronunciation_ready_speech_and_story_direction():
    scenario = normalize_scenario(
        _raw_cinematic_scenario(), ScenarioRequest(target_duration_seconds=40)
    )
    scene = scenario.scenes[0]
    prompt = build_scene_provider_prompt(scene, scenario, has_reference_image=False)
    assert "SCENE TYPE:" in prompt
    assert f"ACT: {scene.act}" in prompt
    assert "NARRATIVE PURPOSE:" in prompt
    assert "STYLE PRESET: cinematic_3d_storytelling" in prompt
    assert "CAMERA:" in prompt
    assert "CONTINUITY:" in prompt
    assert f"{scene.pronunciation_text} " in prompt
    assert "natural Iranian Persian (Farsi) voice-over" in prompt


def test_kie_compatible_scenario_keeps_readable_and_spoken_text_separate():
    scenario = normalize_scenario(
        _raw_cinematic_scenario(), ScenarioRequest(target_duration_seconds=40)
    )
    assert scenario.cta.strip()
    assert scenario.cta_pronunciation_text.strip()
    for scene in scenario.scenes:
        assert scene.narration.strip()
        assert scene.pronunciation_text.strip()
        assert scene.narration in scene.pronunciation_text or scene.pronunciation_text in scene.narration


def test_brand_end_card_uses_rtl_shaping_without_rewriting_latin_text(tmp_path):
    persian = "همین حالا شروع کنید"
    shaped = display_text(persian)
    assert shaped != persian
    assert display_text("https://vandad.ir") == "https://vandad.ir"

    output = tmp_path / "end-card-test.png"
    render_end_card(
        output_path=output, width=640, height=1138,
        title="وندیار", product=_vowelize("معرفی پلتفرم امنیتی"),
        tagline="Intelligent security operations", cta=persian,
        site_url="https://vandad.ir",
    )
    assert output.is_file()
    assert Image.open(output).size == (640, 1138)


def test_long_persian_text_is_wrapped_and_shrunk_inside_safe_area():
    from PIL import ImageDraw

    image = Image.new("RGB", (640, 1138))
    draw = ImageDraw.Draw(image)
    text = " ".join(["عبارت بلند فارسی برای آزمون ایمنی قاب"] * 20)
    lines, font = fit_text_block(
        draw, text, font_name="regular", base_size=60,
        max_width=538, max_height=300, line_gap=10,
    )
    assert len(lines) > 1
    assert font is not None
    for line in wrap_text(draw, text, font, 538):
        assert line


def test_xfade_args_keep_exact_duration_and_mix_native_audio(tmp_path):
    command = video_assembly.build_xfade_concat_args(
        ffmpeg="ffmpeg",
        clip_paths=(Path("a.mp4"), Path("b.mp4")),
        output_path=Path("out.mp4"),
        width=640, height=1138, scene_seconds=(20.0, 20.0),
        transitions=("dissolve",), with_audio=True,
    )
    joined = " ".join(command)
    assert "xfade" in joined
    assert "acrossfade" in joined
    assert "tpad=stop_mode=clone" in joined
    assert "-t 40.000" in joined

    bed = tmp_path / "bed.wav"
    bed.write_bytes(b"RIFFfake")
    music = video_assembly.build_music_mix_args(
        ffmpeg="ffmpeg", video_path=Path("out.mp4"), music_path=bed,
        output_path=Path("mixed.mp4"), total_seconds=40.0, music_gain=0.12,
    )
    joined_music = " ".join(music)
    assert "amix=inputs=2" in joined_music
    assert "volume=0.120" in joined_music
    assert "alimiter" in joined_music


def test_qc_report_fails_closed_on_expected_native_audio_missing(monkeypatch):
    monkeypatch.setattr(
        video_assembly, "probe_video_streams",
        lambda _path: {
            "has_video": True, "has_audio": False, "width": 640, "height": 1138,
            "video_codec": "h264", "audio_codec": None, "duration_seconds": 39.8,
            "audio_stream_count": 0,
        },
    )
    report = video_assembly.qc_video_file(
        Path("final.mp4"), expected_duration_seconds=40, expected_width=640,
        expected_height=1138, expect_audio=True,
    )
    assert report["status"] == "failed"
    assert report["requires_human_review"] is True
    assert any(check["name"] == "native_audio" and not check["passed"] for check in report["checks"])
