"""Deterministic scenario review (scenario quality gate).

Every generated scenario passes this review before it can reach
``script_ready``. The checks are cheap, deterministic, and encode product
rules only: complete structure, duration sanity, Persian narration, and a
no-overclaiming policy that mirrors the scenario system prompt. LLM output is
stochastic, so a review failure must be retryable — the workflow node raises a
transient error and the engine re-generates the scenario.
"""

from __future__ import annotations

import re

from app.schemas.scenario import (
    _DIACRITICS_RE,
    MAX_SCENE_DURATION_SECONDS,
    MIN_SCENE_DURATION_SECONDS,
    Scenario,
    _spoken_tokens,
)

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
_PLATFORM_INTRO_TOKENS = frozenset({"پلاتفرم", "پلتفرم", "سامانه", "سکو"})

_DURATION_TOLERANCE_SECONDS = 1.0
_DUPLICATE_NARRATION_THRESHOLD = 0.8
_DUPLICATE_VISUAL_THRESHOLD = 0.85


def _persian_words_without_diacritics(value: str) -> list[str]:
    """Return multi-letter Persian words that have no pronunciation marks."""
    return [
        word
        for word in _PERSIAN_WORD_RE.findall(value or "")
        if len(word) > 1 and not _DIACRITICS_RE.search(word)
    ]


def _token_set(value: str) -> set[str]:
    """Diacritic-insensitive token set used for duplicate-scene detection."""
    return set(_spoken_tokens(value))


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def review_scenario(
    scenario: Scenario, *, target_duration_seconds: int, language: str = "fa"
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
                _token_set(first.visual_prompt), _token_set(second.visual_prompt)
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

    if language.split("-", 1)[0].strip().lower() == "fa":
        # Every spoken or written Persian surface must really be Persian: the
        # title, the complete narration, the CTA (spoken in single-shot mode)
        # value here means the scenario prompt failed and must be regenerated,
        # never silently submitted to a paid video provider. Brand languages are
        # stored as "fa-IR", so match the language subtag, not the exact string.
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

        diacritic_targets: list[tuple[str, str]] = [
            ("opening_hook", scenario.opening_hook),
            ("cta", scenario.cta or ""),
        ]
        for scene in scenario.scenes:
            diacritic_targets.append((f"scene {scene.index + 1} narration", scene.narration))
        for label, text in diacritic_targets:
            missing = _persian_words_without_diacritics(text)
            if missing:
                sample = "، ".join(missing[:6])
                issues.append(f"{label}: Persian words without diacritics: {sample}.")

    return issues
