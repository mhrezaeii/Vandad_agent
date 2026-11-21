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
from app.services.scenario_service import (
    ScenarioGenerationError,
    build_scenario_prompt,
    parse_scenario_content,
    sanitize_persian_written_surfaces,
)


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
    assert expected_scene_count(15) == 3
    assert expected_scene_count(5) == MIN_SCENES
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


def test_normalize_rejects_empty_scenes():
    request = ScenarioRequest(topic="موضوع تست", target_duration_seconds=30)
    with pytest.raises(ValueError):
        normalize_scenario(_raw_scenario(count=0), request)


def test_normalize_accepts_single_scene_passthrough():
    request = ScenarioRequest(
        topic="موضوع تست", target_duration_seconds=5, scene_count=1
    )
    scenario = normalize_scenario(_raw_scenario(count=1, duration=5.0), request)
    assert len(scenario.scenes) == 1
    assert scenario.total_duration_seconds == pytest.approx(5.0, abs=0.01)


def test_normalize_rejects_scene_without_visual_prompt():
    request = ScenarioRequest(topic="موضوع تست", target_duration_seconds=30)
    raw = _raw_scenario()
    raw["scenes"][2]["visual_prompt"] = "  "
    with pytest.raises(ValueError):
        normalize_scenario(raw, request)


def test_pronunciation_text_preserves_words_and_rejects_rewrites():
    request = ScenarioRequest(topic="موضوع تست", target_duration_seconds=30)
    raw = _raw_scenario(count=2, duration=15.0)
    raw["scenes"][0]["pronunciation_text"] = "رِوایت فارسی صَحنه 1"
    scenario = normalize_scenario(raw, request)
    assert scenario.scenes[0].pronunciation_text == "رِوایت فارسی صَحنه 1"
    raw["scenes"][0]["pronunciation_text"] = "درود فارسی"
    with pytest.raises(ValueError, match="exact narration words"):
        normalize_scenario(raw, request)


def test_production_fa_scenario_requires_vowelized_pronunciation_text():
    request = ScenarioRequest(topic="موضوع تست", target_duration_seconds=30, require_pronunciation_text=True)
    raw = _raw_scenario(count=2, duration=15.0)
    with pytest.raises(ValueError, match="pronunciation_text with diacritics"):
        normalize_scenario(raw, request)
    raw["scenes"][0]["pronunciation_text"] = "رِوایت فارسی صَحنه 1"
    raw["scenes"][1]["pronunciation_text"] = "رِوایت فارسی صَحنه 2"
    raw["cta_pronunciation_text"] = "هَمین حالا شُروع کُنید"
    scenario = normalize_scenario(raw, request)
    assert scenario.scenes[0].pronunciation_text == "رِوایت فارسی صَحنه 1"


def test_storyboard_prompt_requests_vowelized_text_and_uses_brand_lexicon():
    request = ScenarioRequest(
        topic="موضوع تست",
        target_duration_seconds=30,
        pronunciation_lexicon={"وندیار": "وَندیار"},
    )
    system, user = build_scenario_prompt(request)
    assert "pronunciation_text" in user
    assert "اعراب" in system
    assert "وَندیار" in user


def test_storyboard_prompt_allows_only_the_supplied_reference_character():
    request = ScenarioRequest(topic="معرفی محصول", target_duration_seconds=30, has_reference_image=True)
    system, _user = build_scenario_prompt(request)
    assert "تصویر مرجع کاراکتر" in system


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


def test_storyboard_prompt_supports_single_scene_passthrough():
    request = ScenarioRequest(
        topic="معرفی قابلیت امنیتی",
        target_duration_seconds=5,
        scene_count=1,
    )
    _system, user = build_scenario_prompt(request)

    assert "دقیقاً 1" in user
    assert "این پروژه تک‌صحنه است" in user
    assert "problem → product_demo" not in user
    assert "حداقل یک صحنه محصول و یک صحنه CTA لازم است" not in user


def test_storyboard_prompt_includes_verified_platform_knowledge_without_inventing_facts():
    request = ScenarioRequest(
        topic="معرفی خدمات",
        platform_knowledge_context='{"summary":"خدمات مدیریت پروژه","unknowns":["قیمت مشخص نیست"]}',
    )
    system, user = build_scenario_prompt(request)
    assert "قیمت مشخص نیست" in user
    assert "از اطلاعات دانش پلتفرم فقط به‌عنوان مرجع استفاده کن" in system


def test_review_scenario_passes_valid_and_flags_problems():
    from app.services.scenario_review import review_scenario

    request = ScenarioRequest(topic="موضوع تست", target_duration_seconds=30)
    scenario = normalize_scenario(_raw_scenario(count=5, duration=6.0), request)
    assert review_scenario(scenario, target_duration_seconds=30) == []

    overclaiming = normalize_scenario(
        {**_raw_scenario(count=2, duration=15.0), "cta": "تضمین کامل امنیت"},
        request,
    )
    issues = review_scenario(overclaiming, target_duration_seconds=30)
    assert any("forbidden claim" in issue for issue in issues)

    non_persian = normalize_scenario(
        {**_raw_scenario(count=2, duration=15.0), "title": "English only title"},
        request,
    )
    issues = review_scenario(non_persian, target_duration_seconds=30)
    assert any("Persian" in issue for issue in issues)


def test_review_scenario_requires_every_spoken_and_written_surface_in_persian():
    """Title, narration, CTA, on-screen text and subtitles must all be Persian."""
    from app.services.scenario_review import review_scenario

    request = ScenarioRequest(topic="موضوع تست", target_duration_seconds=30)
    raw = _raw_scenario(count=2, duration=15.0)
    english = normalize_scenario(
        {
            **raw,
            "narration": "English only full narration",
            "cta": "Start now",
            "scenes": [
                {**raw["scenes"][0], "on_screen_text": "BUY NOW"},
                {**raw["scenes"][1], "subtitle_text": "English subtitle"},
            ],
        },
        request,
    )

    issues = review_scenario(english, target_duration_seconds=30, language="fa-IR")
    persian_issues = [issue for issue in issues if "must be Persian" in issue]
    assert any(issue.startswith("narration") for issue in persian_issues)
    assert any(issue.startswith("cta") for issue in persian_issues)
    assert any("scene 1 on_screen_text" in issue for issue in persian_issues)
    assert any("scene 2 subtitle_text" in issue for issue in persian_issues)


def test_persian_written_surface_sanitizer_clears_acronym_only_labels():
    """Acronym-only on-screen text is optional copy, not a review failure."""
    from app.services.scenario_review import review_scenario

    request = ScenarioRequest(topic="موضوع تست", target_duration_seconds=30)
    raw = _raw_scenario(count=2, duration=15.0)
    raw["scenes"][0]["on_screen_text"] = "SIEM · SOAR · EDR"
    raw["scenes"][1]["subtitle_text"] = "VISIT VANDEFENCE.IR"
    raw["scenes"][1]["on_screen_text"] = "کنسول امنیت"

    scenario = sanitize_persian_written_surfaces(normalize_scenario(raw, request), request)

    assert scenario.scenes[0].on_screen_text is None
    assert scenario.scenes[1].subtitle_text is None
    assert scenario.scenes[1].on_screen_text == "کنسول امنیت"
    remaining = review_scenario(scenario, target_duration_seconds=30, language="fa-IR")
    assert not [issue for issue in remaining if "on_screen_text" in issue or "subtitle_text" in issue]


def test_persian_written_surface_sanitizer_leaves_non_persian_and_spoken_text():
    request = ScenarioRequest(topic="test topic", target_duration_seconds=30, language="en")
    raw = _raw_scenario(count=2, duration=15.0)
    raw["scenes"][0]["on_screen_text"] = "BUY NOW"
    scenario = normalize_scenario(raw, request)
    assert sanitize_persian_written_surfaces(scenario, request) is scenario

    fa_request = ScenarioRequest(topic="موضوع تست", target_duration_seconds=30)
    spoken = normalize_scenario(_raw_scenario(count=2, duration=15.0), fa_request)
    cleaned = sanitize_persian_written_surfaces(spoken, fa_request)
    assert cleaned.scenes[0].narration == spoken.scenes[0].narration
    assert cleaned.scenes[0].pronunciation_text == spoken.scenes[0].pronunciation_text
    assert cleaned.cta == spoken.cta


def test_persian_prompt_forbids_latin_only_on_screen_text():
    system, _user = build_scenario_prompt(ScenarioRequest(topic="معرفی خدمات"))
    assert "متن صرفاً لاتین ممنوع است" in system
