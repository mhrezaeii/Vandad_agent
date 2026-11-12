"""Scenario schema and parsing tests (no network, no provider calls)."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.schemas.scenario import (
    MAX_SCENES,
    MIN_SCENES,
    Scenario,
    ScenarioRequest,
    expected_scene_count,
    normalize_scenario,
)
from app.services.scenario_service import ScenarioGenerationError, build_scenario_prompt, parse_scenario_content


def _raw_scenario(count: int = 5, duration: float = 6.0) -> dict:
    return {
        "title": "عنوان تست",
        "opening_hook": "یک قلاب روشن برای شروع",
        "visual_style": "clean cinematic vertical",
        "scenes": [
            {
                "index": index,
                "narration": f"روایت فارسی صحنه {index + 1}",
                "visual_prompt": f"English visual prompt {index + 1}",
                "duration_seconds": duration,
                "on_screen_text": f"متن {index + 1}",
                "subtitle_text": f"زیرنویس {index + 1}",
                "scene_type": "product_demo" if index == 2 else ("problem" if index == 0 else "b_roll"),
                "camera_direction": "medium close-up, slow push-in" if index == 1 else "cinematic tracking shot",
                "transition": "match_cut" if index == 2 else "cut",
            }
            for index in range(count)
        ],
        "cta": "همین حالا شروع کنید",
        "caption": "کپشن فارسی",
        "hashtags": ["#امنیت", "#امنیت", "#فناوری"],
    }


def test_expected_scene_count_matches_social_pacing():
    assert expected_scene_count(30) == 6
    assert expected_scene_count(15) == MIN_SCENES
    assert expected_scene_count(120) == MAX_SCENES


def test_request_resolves_scene_count_and_rejects_blank_topic():
    request = ScenarioRequest(topic="موضوع تست", target_duration_seconds=30)
    assert request.resolved_scene_count == 6
    with pytest.raises(ValidationError):
        ScenarioRequest(topic="   ")


def test_normalize_scales_durations_to_exact_target():
    request = ScenarioRequest(topic="موضوع تست", target_duration_seconds=30)
    scenario = normalize_scenario(_raw_scenario(count=5, duration=10.0), request)

    assert isinstance(scenario, Scenario)
    assert len(scenario.scenes) == 5
    assert scenario.total_duration_seconds == pytest.approx(30.0, abs=0.01)
    assert [scene.index for scene in scenario.scenes] == list(range(5))
    # Every scene keeps the required Persian/English fields.
    for scene in scenario.scenes:
        assert scene.narration.startswith("روایت فارسی")
        assert scene.visual_prompt.startswith("English visual prompt")
        assert scene.duration_seconds >= 1.0


def test_normalize_deduplicates_hashtags_and_falls_back_to_request_values():
    request = ScenarioRequest(
        topic="موضوع تست",
        target_duration_seconds=20,
        hashtags=["#یک", "#یک", "#دو"],
    )
    raw = _raw_scenario(count=4, duration=5.0)
    raw["hashtags"] = ["#سه", "#سه"]
    scenario = normalize_scenario(raw, request)
    assert scenario.hashtags == ["#سه"]


def test_normalize_rejects_too_few_scenes():
    request = ScenarioRequest(topic="موضوع تست", target_duration_seconds=30)
    with pytest.raises(ValueError):
        normalize_scenario(_raw_scenario(count=2), request)


def test_normalize_rejects_scene_without_visual_prompt():
    request = ScenarioRequest(topic="موضوع تست", target_duration_seconds=30)
    raw = _raw_scenario()
    raw["scenes"][2]["visual_prompt"] = "  "
    with pytest.raises(ValueError):
        normalize_scenario(raw, request)


def test_parse_scenario_content_accepts_fenced_json():
    request = ScenarioRequest(topic="موضوع تست", target_duration_seconds=30)
    payload = json.dumps(_raw_scenario(), ensure_ascii=False)
    scenario = parse_scenario_content(f"```json\n{payload}\n```", request)
    assert scenario.title == "عنوان تست"
    assert scenario.total_duration_seconds == pytest.approx(30.0, abs=0.01)


def test_parse_scenario_content_rejects_malformed_json():
    request = ScenarioRequest(topic="موضوع تست", target_duration_seconds=30)
    with pytest.raises(ScenarioGenerationError) as excinfo:
        parse_scenario_content("{not json", request)
    assert excinfo.value.code == "scenario_invalid_json"


def test_parse_scenario_content_rejects_empty_response():
    request = ScenarioRequest(topic="موضوع تست", target_duration_seconds=30)
    with pytest.raises(ScenarioGenerationError):
        parse_scenario_content("   ", request)


def test_parse_scenario_content_accepts_dict_payload():
    request = ScenarioRequest(topic="موضوع تست", target_duration_seconds=30)
    scenario = parse_scenario_content(_raw_scenario(), request)
    assert len(scenario.scenes) == 5
    assert scenario.language == "fa-IR"



def test_normalize_preserves_visual_direction_without_character_roles():
    request = ScenarioRequest(topic="معرفی محصول", target_duration_seconds=30)
    scenario = normalize_scenario(_raw_scenario(), request)
    product = scenario.scenes[2]

    assert product.scene_type == "product_demo"
    assert product.camera_direction == "cinematic tracking shot"
    assert product.transition == "match_cut"
    assert not hasattr(product, "speaker")
    assert not hasattr(product, "dialogue")


def test_storyboard_prompt_is_ai_native_and_character_free():
    request = ScenarioRequest(
        topic="معرفی فضای کاری یکپارچه",
        target_duration_seconds=30,
        brand_name="وندیار",
        product_context="Team chat, CRM, Kanban, calendar and AI assistant.",
    )
    system, user = build_scenario_prompt(request)

    assert "product_demo" in system
    assert "کاراکتر اختصاصی" in system
    assert "آواتار سخنگو" in system
    assert "presenter" not in system.lower()
    assert "brand_character" not in user
    assert "Team chat, CRM" in user
    assert "problem → product_demo" in user


def test_storyboard_prompt_includes_verified_platform_knowledge_without_inventing_facts():
    request = ScenarioRequest(
        topic="معرفی خدمات",
        platform_knowledge_context='{"summary":"خدمات مدیریت پروژه","unknowns":["قیمت مشخص نیست"]}',
    )
    system, user = build_scenario_prompt(request)
    assert "قیمت مشخص نیست" in user
    assert "از اطلاعات دانش پلتفرم فقط به‌عنوان مرجع استفاده کن" in system
