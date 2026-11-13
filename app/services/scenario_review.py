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
    MAX_SCENE_DURATION_SECONDS,
    MIN_SCENE_DURATION_SECONDS,
    Scenario,
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

_DURATION_TOLERANCE_SECONDS = 1.0


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

    if language == "fa":
        for position, text in enumerate(
            [scene.narration for scene in scenario.scenes] + [scenario.title], start=1
        ):
            if text.strip() and not _ARABIC_SCRIPT.search(text):
                issues.append(f"text {position} must be Persian but has no Persian characters.")

    return issues
