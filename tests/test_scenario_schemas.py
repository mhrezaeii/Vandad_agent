"""Scenario schema and parsing tests (no network, no provider calls)."""

from __future__ import annotations

import json
import uuid

import pytest

from app.schemas.scenario import (
    MAX_SCENES,
    MIN_SCENES,
    Scenario,
    ScenarioRequest,
    _spoken_tokens,
    expected_scene_count,
    normalize_scenario,
)
from app.services.scenario_review import word_budget_issues
from app.services.scenario_service import (
    ScenarioGenerationError,
    ScenarioService,
    build_scenario_prompt,
    ensure_spoken_diacritics,
    parse_scenario_content,
    sanitize_persian_written_surfaces,
    word_budget_retry_key,
)

_SCENE_WORD_BANKS = tuple(
    words.split()
    for words in (
        (
            "سازمانها هشدارها رویدادها فوریتها خطرها ریسکها ناهنجاریها الگوها "
            "نشانهها شواهد زمینهها جزئیات پیامها فرایندها جریانها ترافیکها "
            "سابقهها تغییرها تفاوتها ارتباطها بررسیها کشفها تشخیصها پیگیریها"
        ),
        (
            "دادهها اطلاعات منبعها ورودیها خروجیها پروندهها فهرستها جدولها "
            "نمودارها شاخصها اندازهها مقادیرها دستهها برچسبها کدها شناسهها "
            "زمانها تاریخها مکانها کاربران نقشها سطوح دسترسیها ذخیرهها"
        ),
        (
            "تیمها اعضا همکاران وظیفهها ترتیبها اولویتها تصمیمها واکنشها "
            "اقدامها فرمانها هماهنگیها جلسهها یادداشتها توضیحها بازخوردها "
            "مهلتها سرعتها دقتها کیفیتها سادگیها روشها گامها نتیجهها تجربهها"
        ),
        (
            "داشبوردها نماها صفحات پنلها جستجوها فیلترها ترتیبها ستونها "
            "ردیفها خلاصهها تصاویر نقشهها مسیرها وضعیتها سلامت عملکرد "
            "ظرفیت مصرف بار پاسخ تاخیر خطاها"
        ),
        (
            "گزارشها اسناد آرشیوها نمونهها مدارک پیوستها محتواها متنها "
            "شرحها تحلیلها ارزیابیها مقایسهها برآوردها پیشبینیها سناریوها "
            "گزینهها انتخابها توصیهها راهکارها خروجیها سوابقها"
        ),
        (
            "پلاتفرم سکو سامانه محیط یکپارچگی همگرایی پیوندها اتصالها "
            "قابلیتها امکانات ویژگیها ابزارها رابطها لایهها موتورها خدماتها "
            "بخشها ساختارها چارچوبها معماریها طراحیها نمایها"
        ),
    )
)


def _filler(bank_index: int, size: int) -> str:
    bank = _SCENE_WORD_BANKS[bank_index]
    return " ".join(bank[position % len(bank)] for position in range(size))


def _scene_narration(index: int, count: int, duration: float) -> str:
    """Fixture narration sized to KIE's measured 116-words-per-30s budget."""
    target = max(4, round(duration * 232 / 60))
    bank = _SCENE_WORD_BANKS[index % len(_SCENE_WORD_BANKS)]
    words = ["رِوایت", "فارِسی", "صَحنه", str(index + 1)]
    while len(words) < target:
        words.extend(bank)
    words = words[:target]
    if count > 1 and index == count - 1:
        words = [*words[:-2], "مُعَرَّفی", "پِلاتفُرم"]
    return " ".join(words) + "."


def _raw_scenario(count: int = 5, duration: float = 6.0) -> dict:
    return {
        "title": "عُنوان تست",
        "opening_hook": "یَک قُلابِ رُوشَن بَرای شُروع",
        "visual_style": "clean cinematic vertical",
        "scenes": [
            {
                "index": index,
                "narration": _scene_narration(index, count, duration),
                "visual_prompt": f"English visual prompt {index + 1}",
                "duration_seconds": duration,
                "on_screen_text": f"مَتن {index + 1}",
                "scene_type": (
                    "product_demo"
                    if index in (2, count - 1)
                    else ("problem" if index == 0 else "b_roll")
                ),
                "camera_direction": "medium close-up, slow push-in" if index == 1 else "cinematic tracking shot",
                "transition": "match_cut" if index == 2 else "cut",
            }
            for index in range(count)
        ],
        "cta": "هَمین لَحظه شُروع کُنید",
        "caption": "کَپشن فارسی",
        "hashtags": ["#امنیت", "#امنیت", "#فناوری"],
    }


def test_expected_scene_count_matches_social_pacing():
    assert expected_scene_count(30) == 6
    assert expected_scene_count(15) == 3
    assert expected_scene_count(5) == MIN_SCENES
    assert expected_scene_count(120) == MAX_SCENES


def test_request_resolves_scene_count():
    request = ScenarioRequest(target_duration_seconds=30)
    assert request.resolved_scene_count == 6


def test_normalize_scales_durations_to_exact_target():
    request = ScenarioRequest(target_duration_seconds=30)
    scenario = normalize_scenario(_raw_scenario(count=5, duration=10.0), request)

    assert isinstance(scenario, Scenario)
    assert len(scenario.scenes) == 5
    assert scenario.total_duration_seconds == pytest.approx(30.0, abs=0.01)
    assert [scene.index for scene in scenario.scenes] == list(range(5))
    # Every scene keeps the required Persian/English fields.
    for scene in scenario.scenes:
        assert scene.narration.startswith("رِوایت فارِسی")
        assert scene.visual_prompt.startswith("English visual prompt")
        assert scene.duration_seconds >= 1.0


def test_normalize_deduplicates_hashtags_and_falls_back_to_request_values():
    request = ScenarioRequest(
        target_duration_seconds=20,
        hashtags=["#یک", "#یک", "#دو"],
    )
    raw = _raw_scenario(count=4, duration=5.0)
    raw["hashtags"] = ["#سه", "#سه"]
    scenario = normalize_scenario(raw, request)
    assert scenario.hashtags == ["#سه"]


def test_normalize_rejects_empty_scenes():
    request = ScenarioRequest(target_duration_seconds=30)
    with pytest.raises(ValueError):
        normalize_scenario(_raw_scenario(count=0), request)


def test_normalize_accepts_single_scene_passthrough():
    request = ScenarioRequest(
        target_duration_seconds=5, scene_count=1
    )
    scenario = normalize_scenario(_raw_scenario(count=1, duration=5.0), request)
    assert len(scenario.scenes) == 1
    assert scenario.total_duration_seconds == pytest.approx(5.0, abs=0.01)


def test_normalize_rejects_scene_without_visual_prompt():
    request = ScenarioRequest(target_duration_seconds=30)
    raw = _raw_scenario()
    raw["scenes"][2]["visual_prompt"] = "  "
    with pytest.raises(ValueError):
        normalize_scenario(raw, request)


def test_pronunciation_text_preserves_words_and_rejects_rewrites():
    request = ScenarioRequest(target_duration_seconds=30)
    raw = _raw_scenario(count=2, duration=15.0)
    pronunciation = raw["scenes"][0]["narration"]
    raw["scenes"][0]["pronunciation_text"] = pronunciation
    scenario = normalize_scenario(raw, request)
    assert scenario.scenes[0].pronunciation_text == pronunciation
    raw["scenes"][0]["pronunciation_text"] = "درود فارسی"
    with pytest.raises(ValueError, match="exact narration words"):
        normalize_scenario(raw, request)


def test_production_fa_scenario_requires_vowelized_pronunciation_text():
    request = ScenarioRequest(target_duration_seconds=30, require_pronunciation_text=True)
    raw = _raw_scenario(count=2, duration=15.0)
    with pytest.raises(ValueError, match="pronunciation_text with diacritics"):
        normalize_scenario(raw, request)
    first_pronunciation = raw["scenes"][0]["narration"]
    second_pronunciation = raw["scenes"][1]["narration"]
    raw["scenes"][0]["pronunciation_text"] = first_pronunciation
    raw["scenes"][1]["pronunciation_text"] = second_pronunciation
    raw["cta_pronunciation_text"] = "هَمین لَحظه شُروع کُنید"
    scenario = normalize_scenario(raw, request)
    assert scenario.scenes[0].pronunciation_text == first_pronunciation


def test_storyboard_prompt_uses_brand_lexicon_without_pronunciation_fields():
    request = ScenarioRequest(
        target_duration_seconds=30,
        pronunciation_lexicon={"وندیار": "وَندیار"},
    )
    system, user = build_scenario_prompt(request)
    assert "pronunciation_text" not in user
    assert "اعراب" in system
    assert "کلمات عادی فارسی" in system
    assert "وَندیار" in user


def test_storyboard_prompt_allows_only_the_supplied_reference_character():
    request = ScenarioRequest(target_duration_seconds=30, has_reference_image=True)
    system, _user = build_scenario_prompt(request)
    assert "تصویر مرجع کاراکتر" in system


def test_parse_scenario_content_accepts_fenced_json():
    request = ScenarioRequest(target_duration_seconds=30)
    payload = json.dumps(_raw_scenario(), ensure_ascii=False)
    scenario = parse_scenario_content(f"```json\n{payload}\n```", request)
    assert scenario.title == "عُنوان تست"
    assert scenario.total_duration_seconds == pytest.approx(30.0, abs=0.01)


def test_parse_scenario_content_rejects_malformed_json():
    request = ScenarioRequest(target_duration_seconds=30)
    with pytest.raises(ScenarioGenerationError) as excinfo:
        parse_scenario_content("{not json", request)
    assert excinfo.value.code == "scenario_invalid_json"


def test_parse_scenario_content_rejects_empty_response():
    request = ScenarioRequest(target_duration_seconds=30)
    with pytest.raises(ScenarioGenerationError):
        parse_scenario_content("   ", request)


def test_parse_scenario_content_accepts_dict_payload():
    request = ScenarioRequest(target_duration_seconds=30)
    scenario = parse_scenario_content(_raw_scenario(), request)
    assert len(scenario.scenes) == 5
    assert scenario.language == "fa-IR"



def test_normalize_preserves_visual_direction_without_character_roles():
    request = ScenarioRequest(target_duration_seconds=30)
    scenario = normalize_scenario(_raw_scenario(), request)
    product = scenario.scenes[2]

    assert product.scene_type == "product_demo"
    assert product.camera_direction == "cinematic tracking shot"
    assert product.transition == "match_cut"
    assert not hasattr(product, "speaker")
    assert not hasattr(product, "dialogue")


def test_storyboard_prompt_is_ai_native_and_character_free():
    request = ScenarioRequest(
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
    assert "چکیدهای از کل پلتفرم" in system
    assert "موضوع:" not in user


def test_storyboard_prompt_supports_single_scene_passthrough():
    request = ScenarioRequest(
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
        platform_knowledge_context='{"summary":"خدمات مدیریت پروژه","unknowns":["قیمت مشخص نیست"]}',
    )
    system, user = build_scenario_prompt(request)
    assert "قیمت مشخص نیست" in user
    assert "از اطلاعات دانش پلتفرم فقط به‌عنوان مرجع استفاده کن" in system


def test_review_scenario_passes_valid_and_flags_problems():
    from app.services.scenario_review import review_scenario

    request = ScenarioRequest(target_duration_seconds=30)
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
    """Title, narration, CTA and on-screen text must all be Persian."""
    from app.services.scenario_review import review_scenario

    request = ScenarioRequest(target_duration_seconds=30)
    raw = _raw_scenario(count=2, duration=15.0)
    english = normalize_scenario(
        {
            **raw,
            "narration": "English only full narration",
            "cta": "Start now",
            "scenes": [
                {**raw["scenes"][0], "on_screen_text": "BUY NOW"},
                {**raw["scenes"][1], "on_screen_text": "BUY NOW"},
            ],
        },
        request,
    )

    issues = review_scenario(english, target_duration_seconds=30, language="fa-IR")
    persian_issues = [issue for issue in issues if "must be Persian" in issue]
    assert any(issue.startswith("narration") for issue in persian_issues)
    assert any(issue.startswith("cta") for issue in persian_issues)
    assert any("scene 1 on_screen_text" in issue for issue in persian_issues)
    assert any("scene 2 on_screen_text" in issue for issue in persian_issues)


def test_persian_written_surface_sanitizer_clears_every_on_screen_label():
    """No on-screen text survives, Persian or Latin, because captions are unreliable."""
    from app.services.scenario_review import review_scenario

    request = ScenarioRequest(target_duration_seconds=30)
    raw = _raw_scenario(count=2, duration=15.0)
    raw["scenes"][0]["on_screen_text"] = "SIEM · SOAR · EDR"
    raw["scenes"][1]["on_screen_text"] = "کنسول امنیت"

    scenario = sanitize_persian_written_surfaces(normalize_scenario(raw, request), request)

    assert all(scene.on_screen_text is None for scene in scenario.scenes)
    remaining = review_scenario(scenario, target_duration_seconds=30, language="fa-IR")
    assert not [issue for issue in remaining if "on_screen_text" in issue]


def test_persian_written_surface_sanitizer_leaves_non_persian_and_spoken_text():
    request = ScenarioRequest(target_duration_seconds=30, language="en")
    raw = _raw_scenario(count=2, duration=15.0)
    raw["scenes"][0]["on_screen_text"] = "BUY NOW"
    scenario = normalize_scenario(raw, request)
    assert sanitize_persian_written_surfaces(scenario, request) is scenario

    fa_request = ScenarioRequest(target_duration_seconds=30)
    spoken = normalize_scenario(_raw_scenario(count=2, duration=15.0), fa_request)
    cleaned = sanitize_persian_written_surfaces(spoken, fa_request)
    assert cleaned.scenes[0].narration == spoken.scenes[0].narration
    assert cleaned.scenes[0].pronunciation_text == spoken.scenes[0].pronunciation_text
    assert cleaned.cta == spoken.cta


def test_persian_prompt_forbids_all_on_screen_text():
    system, user = build_scenario_prompt(ScenarioRequest())
    assert "هیچ متنی روی تصویر رندر نشود" in system
    assert "on_screen_text" in user


def test_storyboard_prompt_requires_diacritics_unique_scenes_and_platform_finale():
    system, user = build_scenario_prompt(ScenarioRequest(target_duration_seconds=30))
    assert "اعراب" in system
    assert "کلمات عادی فارسی" in user
    assert "تکرار" in system
    assert "نزدیک به تکرار" in user
    assert "آخرین صحنه" in user
    assert "معرفی کلی پلتفرم" in user
    assert "بعد از CTA هیچ جمله" in user
    assert "scene_type=cta" in user


def test_review_scenario_flags_duplicate_scenes():
    from app.services.scenario_review import review_scenario

    request = ScenarioRequest(target_duration_seconds=30)
    raw = _raw_scenario(count=2, duration=15.0)
    raw["scenes"][1]["narration"] = raw["scenes"][0]["narration"]
    raw["scenes"][1]["visual_prompt"] = raw["scenes"][0]["visual_prompt"]
    issues = review_scenario(normalize_scenario(raw, request), target_duration_seconds=30)
    assert any("duplicate" in issue for issue in issues)


def test_review_scenario_requires_last_scene_to_introduce_the_platform():
    from app.services.scenario_review import review_scenario

    request = ScenarioRequest(target_duration_seconds=30)
    raw = _raw_scenario(count=2, duration=15.0)
    raw["scenes"][-1]["scene_type"] = "b_roll"
    raw["scenes"][-1]["narration"] = "رِوایت فارِسی صَحنه 2"
    issues = review_scenario(normalize_scenario(raw, request), target_duration_seconds=30)
    assert any("last scene" in issue for issue in issues)


def test_review_scenario_flags_unvowelized_hard_words_only():
    from app.services.scenario_review import review_scenario

    request = ScenarioRequest(target_duration_seconds=30)
    raw = _raw_scenario(count=2, duration=15.0)
    raw["scenes"][0]["narration"] = "سیستم پایش پروژه بدون اعراب"
    issues = review_scenario(normalize_scenario(raw, request), target_duration_seconds=30)
    diacritic_issues = [issue for issue in issues if "hard-to-pronounce" in issue]
    assert any("سیستم" in issue for issue in diacritic_issues)
    assert any("پایش" in issue for issue in diacritic_issues)
    assert any("پروژه" in issue for issue in diacritic_issues)


def test_spoken_diacritics_fixer_marks_only_hard_words_and_review_passes():
    from app.services.scenario_review import review_scenario

    request = ScenarioRequest(target_duration_seconds=30)
    raw = _raw_scenario(count=2, duration=15.0)
    raw["opening_hook"] = "وقتی کارها پراکندهاند، دید کلی دشوار میشود."
    raw["cta"] = "همین حالا شروع کنید"
    raw["scenes"][0]["narration"] = (
        "کارها و هماهنگی تیم، گاهی در جاهای گوناگون پراکنده میشوند. "
        + " " + _filler(2, 49)
    )
    raw["scenes"][1]["narration"] = (
        "وندیار، سیستم عاملی برای مدیریت پروژه و تیم است. "
        + " " + _filler(0, 49)
    )

    scenario = ensure_spoken_diacritics(normalize_scenario(raw, request), request)

    # Ordinary Persian words stay unvowelized.
    assert "کارها و هماهنگی تیم" in scenario.scenes[0].narration
    assert "پراکنده میشوند" in scenario.scenes[0].narration
    # Only hard/brand words receive pronunciation marks.
    assert "وَندیار" in scenario.scenes[1].narration
    assert "سیستِم" in scenario.scenes[1].narration
    assert "عامِلی" in scenario.scenes[1].narration
    assert "مُدیریّت" in scenario.scenes[1].narration
    assert "پِروژه" in scenario.scenes[1].narration
    assert review_scenario(scenario, target_duration_seconds=30) == []


def test_storyboard_prompt_states_exact_word_budget():
    _system, user = build_scenario_prompt(ScenarioRequest(target_duration_seconds=20))

    assert "دقیقاً 5.0 ثانیه" in user
    assert "هدف گفتار: 69" in user
    assert "هدف 17" in user
    assert "حداقل 65" in user
    assert "حداکثر 77" in user
    assert "16 تا 19" in user
    assert "بودجه گفتار: حدود" not in user


def test_word_budget_retry_key_is_distinct_and_stable():
    company_id = uuid.uuid4()
    first = word_budget_retry_key(
        "content-scenario:1", company_id=company_id, user="feedback A", attempt=1
    )
    second = word_budget_retry_key(
        "content-scenario:1", company_id=company_id, user="feedback B", attempt=1
    )
    second_attempt = word_budget_retry_key(
        "content-scenario:1", company_id=company_id, user="feedback A", attempt=2
    )

    assert first.startswith("content-scenario:1:word-budget-repair-1:")
    assert first != second
    assert first != second_attempt
    assert first == word_budget_retry_key(
        "content-scenario:1", company_id=company_id, user="feedback A", attempt=1
    )
    assert word_budget_retry_key(
        None, company_id=company_id, user="feedback A", attempt=1
    ).startswith(f"auto:{company_id}:word-budget-repair-1:")


async def test_scenario_service_repairs_word_budget_with_counted_feedback():
    request = ScenarioRequest(target_duration_seconds=20)
    overlong = _raw_scenario(count=4, duration=5.0)
    for index, scene in enumerate(overlong["scenes"]):
        scene["narration"] = _filler(index, 25) + "."
    corrected = _raw_scenario(count=4, duration=5.0)

    calls: list[dict[str, str]] = []

    async def llm_call(*, system: str, user: str) -> dict[str, object]:
        calls.append({"system": system, "user": user})
        return {"content": overlong if len(calls) == 1 else corrected}

    service = ScenarioService(None, None, None, None, llm_call=llm_call)
    scenario, metadata = await service.generate(
        company_id=uuid.uuid4(),
        connection_id=None,
        request=request,
        idempotency_key="content-scenario:test",
    )

    assert len(calls) == 2
    assert "100" in calls[1]["user"]
    assert "77" in calls[1]["user"]
    assert "25 واژه" in calls[1]["user"]
    assert metadata["content"] is corrected
    assert len(_spoken_tokens(scenario.narration)) == 76
    assert word_budget_issues(scenario, target_duration_seconds=20) == []


async def test_scenario_service_uses_second_correction_for_still_overlong_draft():
    request = ScenarioRequest(target_duration_seconds=20)
    overlong = _raw_scenario(count=4, duration=5.0)
    for index, scene in enumerate(overlong["scenes"]):
        scene["narration"] = _filler(index, 25) + "."
    still_over = _raw_scenario(count=4, duration=5.0)
    for index, (scene, size) in enumerate(
        zip(still_over["scenes"], (21, 21, 20, 21), strict=True)
    ):
        scene["narration"] = _filler(index, size) + "."
    corrected = _raw_scenario(count=4, duration=5.0)

    responses = [overlong, still_over, corrected]
    calls: list[dict[str, str]] = []

    async def llm_call(*, system: str, user: str) -> dict[str, object]:
        calls.append({"system": system, "user": user})
        return {"content": responses[len(calls) - 1]}

    service = ScenarioService(None, None, None, None, llm_call=llm_call)
    scenario, metadata = await service.generate(
        company_id=uuid.uuid4(),
        connection_id=None,
        request=request,
        idempotency_key="content-scenario:test",
    )

    assert len(calls) == 3
    assert "83" in calls[2]["user"]
    assert "هدف: 69" in calls[2]["user"]
    assert "هدف: 17" in calls[2]["user"]
    assert "تلاش قبلی" in calls[2]["user"]
    assert metadata["content"] is corrected
    assert len(_spoken_tokens(scenario.narration)) == 76
    assert word_budget_issues(scenario, target_duration_seconds=20) == []


async def test_scenario_service_raises_transient_when_budget_cannot_be_fixed():
    request = ScenarioRequest(target_duration_seconds=20)
    overlong = _raw_scenario(count=4, duration=5.0)
    for index, scene in enumerate(overlong["scenes"]):
        scene["narration"] = _filler(index, 25) + "."

    calls: list[dict[str, str]] = []

    async def llm_call(*, system: str, user: str) -> dict[str, object]:
        calls.append({"system": system, "user": user})
        return {"content": overlong}

    service = ScenarioService(None, None, None, None, llm_call=llm_call)
    with pytest.raises(ScenarioGenerationError) as excinfo:
        await service.generate(
            company_id=uuid.uuid4(),
            connection_id=None,
            request=request,
            idempotency_key="content-scenario:test",
        )

    assert len(calls) == 3
    assert excinfo.value.code == "scenario_word_budget"
    assert excinfo.value.retryable is True
