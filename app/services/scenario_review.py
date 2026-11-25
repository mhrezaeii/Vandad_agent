"""Deterministic scenario review (scenario quality gate).

Every generated scenario passes this review before it can reach
``script_ready``. The checks are cheap, deterministic, and encode product
rules only: complete structure, duration and speech budget, Persian narration,
selective (hard-word-only) diacritics, distinct beats, and a single terminal
CTA. LLM output is stochastic, so a review failure must be retryable — the
workflow node raises a transient error and the engine re-generates the
scenario.
"""

from __future__ import annotations

import re

from app.schemas.scenario import (
    _DIACRITICS_RE,
    MAX_NARRATION_WORDS_PER_MINUTE,
    MAX_SCENE_DURATION_SECONDS,
    MIN_SCENE_DURATION_SECONDS,
    Scenario,
    _spoken_tokens,
    hard_pronunciation_fixes,
    max_spoken_words,
    min_spoken_words,
)
from app.services.scene_novelty import normalize_visual_signature

# Unverifiable or absolute claims are forbidden by the scenario system prompt;
# the review enforces the same policy on the produced text.
FORBIDDEN_CLAIM_PATTERNS: tuple[str, ...] = (
    "تضمین",
    "۱۰۰٪",
    "100%",
    "صد در صد",
    "قطعاً",
    "قطعا",
    "بدون هیچ خطری",
    "بدون هیچ ریسکی",
    "هرگز هک نمی",
)

_ARABIC_SCRIPT = re.compile(r"[\u0600-\u06FF]")
_PERSIAN_WORD_RE = re.compile(r"[\u0600-\u06FF]+")
_PLATFORM_INTRO_TOKENS = frozenset(
    {"پلاتفرم", "پلتفرم", "سامانه", "سکو", "سیستم", "سیستمها"}
)

_DURATION_TOLERANCE_SECONDS = 1.0
_DUPLICATE_NARRATION_THRESHOLD = 0.7
_DUPLICATE_VISUAL_THRESHOLD = 0.8
_CTA_OVERLAP_THRESHOLD = 0.6
_WORD_BUDGET_GRACE = 1


def _token_set(value: str) -> set[str]:
    """Diacritic-insensitive token set used for duplicate-scene detection."""
    return set(_spoken_tokens(value))


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _visual_key(value: str) -> set[str]:
    return set(_spoken_tokens(normalize_visual_signature(value)))


def _missing_hard_word_diacritics(value: str, fixes: dict[str, str]) -> list[str]:
    """Hard words present in the text but still missing pronunciation marks."""
    missing: set[str] = set()
    for word in _PERSIAN_WORD_RE.findall(value or ""):
        if _DIACRITICS_RE.search(word):
            continue
        for plain in fixes:
            if word == plain or word.startswith(plain):
                missing.add(plain)
    return sorted(missing)


def _mentions_cta(value: str, cta_key: tuple[str, ...]) -> bool:
    """Detect a spoken CTA, including a partial overlap with the closing line."""
    tokens = tuple(_spoken_tokens(value))
    if not tokens or not cta_key:
        return False
    width = len(cta_key)
    if width <= len(tokens):
        for index in range(len(tokens) - width + 1):
            if tokens[index : index + width] == cta_key:
                return True
    return _jaccard(set(tokens), set(cta_key)) >= _CTA_OVERLAP_THRESHOLD


def word_budget_issues(
    scenario: Scenario,
    *,
    target_duration_seconds: int | float,
) -> list[str]:
    """Return word-budget issues; generation reuses these exact thresholds."""
    issues: list[str] = []
    if target_duration_seconds:
        total_words = len(_spoken_tokens(scenario.narration))
        total_limit = max_spoken_words(float(target_duration_seconds))
        total_minimum = min_spoken_words(float(target_duration_seconds))
        if total_words > total_limit + _WORD_BUDGET_GRACE:
            issues.append(
                f"narration has {total_words} words for {target_duration_seconds}s; "
                f"maximum is {total_limit} at the measured "
                f"{MAX_NARRATION_WORDS_PER_MINUTE} wpm rate."
            )
        elif total_words < total_minimum:
            issues.append(
                f"narration has only {total_words} words for {target_duration_seconds}s; "
                f"at least {total_minimum} are needed at the measured "
                f"{MAX_NARRATION_WORDS_PER_MINUTE} wpm rate, otherwise the model "
                "fills the silence with repeated scenes."
            )
    for scene in sorted(scenario.scenes, key=lambda item: item.index):
        words = len(_spoken_tokens(scene.narration))
        limit = max_spoken_words(scene.duration_seconds)
        minimum = min_spoken_words(scene.duration_seconds)
        wpm = round(words / scene.duration_seconds * 60) if scene.duration_seconds else 0
        if words > limit + _WORD_BUDGET_GRACE:
            issues.append(
                f"scene {scene.index + 1} narration is ~{wpm} wpm, above the "
                f"measured {MAX_NARRATION_WORDS_PER_MINUTE} wpm rate."
            )
        elif words < minimum:
            issues.append(
                f"scene {scene.index + 1} narration has only {words} words for "
                f"{scene.duration_seconds:g}s; at least {minimum} are needed to "
                "prevent the model from improvising a repeated scene."
            )
    return issues


def review_scenario(
    scenario: Scenario,
    *,
    target_duration_seconds: int | float,
    language: str = "fa",
    pronunciation_lexicon: dict[str, str] | None = None,
) -> list[str]:
    """Return the review issues; an empty list means the scenario passes."""
    issues: list[str] = []

    if not scenario.title or not scenario.title.strip():
        issues.append("title is empty.")
    if not scenario.scenes:
        issues.append("scenario has no scenes.")

    for scene in scenario.scenes:
        where = f"scene {scene.index + 1}"
        if not scene.narration.strip():
            issues.append(f"{where}: narration is empty.")
        if not scene.visual_prompt.strip():
            issues.append(f"{where}: visual_prompt is empty.")
        if not MIN_SCENE_DURATION_SECONDS <= scene.duration_seconds <= MAX_SCENE_DURATION_SECONDS:
            issues.append(
                f"{where}: duration {scene.duration_seconds:g}s is outside "
                f"[{MIN_SCENE_DURATION_SECONDS:g}, {MAX_SCENE_DURATION_SECONDS:g}]s."
            )
        for label, text in (
            ("narration", scene.narration),
            ("on_screen_text", scene.on_screen_text or ""),
        ):
            for pattern in FORBIDDEN_CLAIM_PATTERNS:
                if pattern in text:
                    issues.append(f"{where}: {label} contains the forbidden claim '{pattern}'.")

    for label, text in (("title", scenario.title), ("cta", scenario.cta or "")):
        for pattern in FORBIDDEN_CLAIM_PATTERNS:
            if pattern in text:
                issues.append(f"{label} contains the forbidden claim '{pattern}'.")

    total = sum(scene.duration_seconds for scene in scenario.scenes)
    if target_duration_seconds and abs(total - target_duration_seconds) > _DURATION_TOLERANCE_SECONDS:
        issues.append(
            f"scene durations total {total:.1f}s but the target is {target_duration_seconds}s."
        )

    ordered_scenes = sorted(scenario.scenes, key=lambda item: item.index)
    for position, first in enumerate(ordered_scenes):
        for second in ordered_scenes[position + 1 :]:
            pair = f"scene {first.index + 1} and scene {second.index + 1}"
            if _jaccard(_token_set(first.narration), _token_set(second.narration)) >= _DUPLICATE_NARRATION_THRESHOLD:
                issues.append(f"{pair}: duplicate or near-duplicate narration.")
            visual_overlap = _jaccard(
                _visual_key(first.visual_prompt), _visual_key(second.visual_prompt)
            )
            if visual_overlap >= _DUPLICATE_VISUAL_THRESHOLD:
                issues.append(f"{pair}: duplicate or near-duplicate visual_prompt.")

    if len(ordered_scenes) > 1:
        last = ordered_scenes[-1]
        if last.scene_type != "product_demo":
            issues.append("last scene must introduce the platform with scene_type 'product_demo'.")
        if not _token_set(last.narration).intersection(_PLATFORM_INTRO_TOKENS):
            issues.append(
                "last scene narration must introduce the platform instead of repeating the CTA "
                "or focusing on one narrow feature."
            )

    # Calm delivery: a beat must not contain more words than can be spoken at
    # the approved pace. Otherwise the video model rushes and improvises a new
    # line after the script ends. The same helper drives the writer's repair
    # retry, so generation cannot pass a script the review would reject.
    issues.extend(
        word_budget_issues(scenario, target_duration_seconds=target_duration_seconds)
    )

    # One CTA, at the very end. Nothing may continue after the final beat.
    cta_key = tuple(_spoken_tokens(scenario.cta or ""))
    if len(ordered_scenes) > 1:
        for scene in ordered_scenes:
            if scene.scene_type == "cta":
                issues.append(
                    f"scene {scene.index + 1}: scene_type 'cta' is not allowed in a "
                    "multi-scene storyboard; the CTA belongs only in the cta field."
                )
        for position, scene in enumerate(ordered_scenes):
            if _mentions_cta(scene.narration, cta_key):
                issues.append(
                    f"scene {scene.index + 1} repeats the CTA; it must be spoken once at the end."
                )
                if position < len(ordered_scenes) - 1:
                    issues.append("continuation after the final CTA beat is not allowed.")
    if ordered_scenes:
        joined = tuple(_spoken_tokens(" ".join(scene.narration for scene in ordered_scenes)))
        full = tuple(_spoken_tokens(scenario.narration))
        if full and joined and full != joined:
            issues.append(
                "scenario narration must contain exactly the ordered scene narrations; "
                "no extra speech after the final beat."
            )

    if language.split("-", 1)[0].strip().lower() == "fa":
        # Every spoken or written Persian surface must really be Persian. A
        # failure here means the scenario prompt failed and must be regenerated,
        # never silently submitted to a paid video provider. Brand languages
        # are stored as "fa-IR", so match the language subtag.
        persian_targets: list[tuple[str, str]] = [
            ("title", scenario.title),
            ("narration", scenario.narration),
            ("cta", scenario.cta or ""),
            ("cta_pronunciation_text", scenario.cta_pronunciation_text or scenario.cta or ""),
        ]
        for scene in scenario.scenes:
            where = f"scene {scene.index + 1}"
            persian_targets.extend(
                [
                    (f"{where} narration", scene.narration),
                    (f"{where} pronunciation_text", scene.pronunciation_text or scene.narration),
                    (f"{where} on_screen_text", scene.on_screen_text or ""),
                ]
            )
        for label, text in persian_targets:
            if text.strip() and not _ARABIC_SCRIPT.search(text):
                issues.append(f"{label} must be Persian but has no Persian characters.")

        # Diacritics are a pronunciation aid for hard/brand words only. Ordinary
        # words must stay unvowelized, so the gate checks required marks rather
        # than a global vowelized-word ratio.
        fixes = hard_pronunciation_fixes(pronunciation_lexicon)
        diacritic_targets: list[tuple[str, str]] = [
            ("opening_hook", scenario.opening_hook),
            ("narration", scenario.narration),
            ("cta", scenario.cta or ""),
        ]
        for scene in scenario.scenes:
            diacritic_targets.append((f"scene {scene.index + 1} narration", scene.narration))
        for label, text in diacritic_targets:
            missing = _missing_hard_word_diacritics(text, fixes)
            if missing:
                issues.append(
                    f"{label}: hard-to-pronounce words need diacritics: "
                    f"{', '.join(missing[:6])}."
                )

    return issues
