"""Single-shot prompt composer tests: one scenario -> one complete prompt."""
from __future__ import annotations

import pytest

from app.schemas.scenario import Scenario, SceneSpec
from app.services.prompt_composer import (
    PromptCompositionError,
    compose_single_shot_prompt,
)


def _scenario(scene_count: int = 3, target: int = 20) -> Scenario:
    scenes = []
    per_scene = target / scene_count
    kinds = ["problem", "product_demo", "b_roll", "motion_graphic", "cta"]
    for index in range(scene_count):
        scenes.append(
            SceneSpec(
                index=index,
                narration=f"جملهٔ روایت شمارهٔ {index + 1} به فارسی.",
                visual_prompt=(
                    f"The same young office worker (round glasses, red tie) "
                    f"interacts with a floating productivity dashboard, beat {index + 1}."
                ),
                duration_seconds=per_scene,
                on_screen_text=f"متن روی تصویر {index + 1}" if index == 0 else None,
                subtitle_text=f"زیرنویس {index + 1}",
                scene_type=kinds[index % len(kinds)],
                camera_direction="Slow push-in" if index == 0 else None,
                transition="cut",
            )
        )
    return Scenario(
        title="معرفی محصول",
        opening_hook=scenes[0].narration,
        scenes=scenes,
        narration=" ".join(scene.narration for scene in scenes),
        cta="همین حالا شروع کنید.",
        caption="کپشن ویدیو",
        hashtags=["#وندیار"],
        target_duration_seconds=target,
        visual_style="Pixar-style 3D animation, warm lighting",
        language="fa-IR",
    )


def test_multi_beat_scenario_becomes_one_prompt_preserving_order():
    scenario = _scenario(scene_count=4)
    prompt = compose_single_shot_prompt(scenario, max_characters=5000)

    assert prompt.count("Beat ") == 4
    positions = [prompt.find(f"Beat {index + 1} ") for index in range(4)]
    assert positions == sorted(positions)
    # Every beat's visual direction and exact Persian narration survive.
    for scene in scenario.scenes:
        assert scene.visual_prompt in prompt
        assert scene.narration in prompt
    assert scenario.cta in prompt


def test_prompt_includes_persian_speech_and_onscreen_enforcement():
    prompt = compose_single_shot_prompt(_scenario(), max_characters=5000)

    assert "Iranian Persian" in prompt
    assert "Do NOT speak English" in prompt
    assert "Do NOT translate, paraphrase, shorten or omit" in prompt
    assert "synchronised" in prompt
    assert "right-to-left" in prompt
    assert "متن روی تصویر 1" in prompt


def test_prompt_includes_character_consistency_instructions():
    prompt = compose_single_shot_prompt(
        _scenario(),
        character_description="young office worker, round glasses, red tie",
        max_characters=5000,
    )
    assert "CHARACTER CONSISTENCY" in prompt
    assert "young office worker, round glasses, red tie" in prompt
    assert "reference image" in prompt
    assert "Never introduce a different person" in prompt


def test_prompt_without_character_description_has_no_consistency_block():
    prompt = compose_single_shot_prompt(_scenario(), max_characters=5000)
    assert "CHARACTER CONSISTENCY" not in prompt


def test_narration_is_never_duplicated_in_prompt_sections():
    scenario = _scenario()
    prompt = compose_single_shot_prompt(scenario, max_characters=5000)
    # Each beat's narration appears exactly once; no separate narration dump.
    for scene in scenario.scenes:
        assert prompt.count(scene.narration) == 1


def test_oversized_scenario_fails_clearly_without_truncation():
    scenario = _scenario(scene_count=12, target=30)
    with pytest.raises(PromptCompositionError) as error:
        compose_single_shot_prompt(scenario, max_characters=300)
    assert error.value.code == "prompt_too_long"
    assert "never cut silently" in error.value.message or "truncating" in error.value.message


def test_scene_durations_are_stated_for_model_pacing():
    prompt = compose_single_shot_prompt(_scenario(scene_count=2, target=20), max_characters=5000)
    assert "Beat 1 (10s)" in prompt
    assert "Beat 2 (10s)" in prompt
