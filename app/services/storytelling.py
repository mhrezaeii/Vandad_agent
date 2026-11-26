"""Storytelling engine: style presets, character identity and storyboard QC.

Everything here is deterministic and provider-neutral. It turns a validated
scenario into a directed storyboard that can be reviewed before any paid KIE
request is submitted, and produces the compact identity/style blocks consumed
by prompt composition.
"""

from __future__ import annotations

from typing import Any

from app.schemas.scenario import Scenario, SceneSpec
from app.schemas.storytelling import (
    ACT_PURPOSE,
    STORYBOARD_SCENE_TYPES,
    CharacterBible,
    Storyboard,
    StoryboardScene,
    allocate_act_durations,
    build_character_bible,
)

DEFAULT_STYLE_PRESET = "cinematic_3d_storytelling"


StylePreset = dict[str, Any]


STYLE_PRESETS: dict[str, dict[str, Any]] = {
    "cinematic_3d_storytelling": {
        "visual_style": (
            "Premium stylised 3D animation with expressive human faces, detailed "
            "environments, natural character movement and professional cinematic "
            "lighting. Soft global illumination, realistic materials, high-quality "
            "shadows, subtle depth of field and rich but controlled colour grading. "
            "Vertical 9:16 composition, carefully composed frames, smooth motivated "
            "camera movement and professional animation timing."
        ),
        "avoid": (
            "No flat stock-video look, no random scene changes, no repeated office "
            "shots, no drifting characters, no meaningless floating icons, no "
            "abstract filler backgrounds, no distorted faces or hands, no flicker, "
            "no unnatural movement and no inconsistent environment geometry."
        ),
        "lighting": "soft cinematic key light, gentle rim light, realistic contact shadows",
        "camera": "motivated wide/medium/close coverage with slow, purposeful movement",
        "animation": "natural weight, believable gestures, clean holds between actions",
    },
    "clean_cinematic": {
        "visual_style": "Clean modern cinematic product film with restrained colour and realistic lighting.",
        "avoid": "No cluttered frames, no decorative motion, no unreadable UI text.",
        "lighting": "soft neutral studio lighting",
        "camera": "steady tripod and gentle push-ins",
        "animation": "minimal, precise motion",
    },
    "motion_graphic": {
        "visual_style": "Flat-but-premium motion graphics with brand-coloured geometric forms.",
        "avoid": "No photographic humans, no generated lettering, no random icon storms.",
        "lighting": "graphic, evenly lit",
        "camera": "orthographic 2D camera moves",
        "animation": "smooth easing and deliberate reveals",
    },
}


def get_style_preset(name: str | None) -> dict[str, Any]:
    """Resolve a preset name, falling back to the primary cinematic preset."""
    key = (name or "").strip()
    return STYLE_PRESETS.get(key, STYLE_PRESETS[DEFAULT_STYLE_PRESET])


def style_block(name: str | None, *, include_avoid: bool = True) -> str:
    preset = get_style_preset(name)
    parts = [f"VISUAL STYLE: {preset['visual_style']}"]
    if include_avoid:
        parts.append(f"AVOID: {preset['avoid']}")
    return " ".join(parts)


def character_block(bible: CharacterBible | None) -> str:
    """Stable cross-scene identity instructions for all recurring characters."""
    if bible is None or not bible.characters:
        return ""
    lines = ["CHARACTER BIBLE (identity locked across every scene):"]
    for spec in bible.characters:
        identity = spec.identity_description()
        reference = (
            f" Reference image: {spec.reference_image_key}."
            if spec.reference_image_key else ""
        )
        lines.append(f"- {spec.character_id}: {identity}.{reference}")
    for relation in bible.relationships:
        lines.append(f"- RELATIONSHIP: {relation}")
    for rule in bible.continuity_rules:
        lines.append(f"- CONTINUITY: {rule}")
    lines.append(
        "Never substitute, restyle, age, re-clothe or redesign a character "
        "between scenes without an explicit story reason."
    )
    return "\n".join(lines)


def _scene_direction(scene: SceneSpec, act: str, purpose: str) -> dict[str, Any]:
    """Derive missing storyboard direction from existing scenario fields."""
    visual = scene.visual_prompt.strip()
    return {
        "scene_id": scene.scene_id or f"scene-{scene.index + 1:02d}",
        "act": act,
        "narrative_purpose": purpose or ACT_PURPOSE[act],
        "scene_type": scene.scene_type,
        "duration_seconds": scene.duration_seconds,
        "characters": list(scene.characters),
        "character_actions": scene.character_actions or visual,
        "character_emotions": scene.character_emotions or "",
        "environment": scene.environment or "",
        "camera_shot": scene.camera_shot or "medium shot",
        "camera_angle": scene.camera_angle or "eye level",
        "camera_movement": scene.camera_movement or (
            scene.camera_direction or "static, intentional framing"
        ),
        "lighting": scene.lighting or get_style_preset(None)["lighting"],
        "visual_composition": scene.visual_composition or visual,
        "transition_in": scene.transition_in or scene.transition or "cut",
        "transition_out": scene.transition_out or "cut",
        "narration": scene.narration,
        "pronunciation_text": scene.pronunciation_text or scene.narration,
        "sound_design": scene.sound_design or "soft corporate bed, narration forward",
        "visual_prompt": visual,
        "continuity_constraints": list(scene.continuity_constraints)
        or ["keep character identity, wardrobe and environment geometry unchanged"],
    }


def build_storyboard(
    scenario: Scenario,
    *,
    style_preset: str | None = None,
    character_bible: CharacterBible | None = None,
) -> Storyboard:
    """Build a validated storyboard from a normalized scenario.

    New fields are used when the writer supplied them; older scenarios are
    upgraded deterministically, so the engine is backward compatible.
    """
    bible = character_bible or scenario.character_bible or CharacterBible()
    ordered = sorted(scenario.scenes, key=lambda item: item.index)
    if any(scene.act for scene in ordered):
        allocations = [(scene.act or "problem", scene.duration_seconds) for scene in ordered]
    else:
        allocations = allocate_act_durations(
            scenario.total_duration_seconds, len(ordered)
        )

    scenes: list[StoryboardScene] = []
    for scene, (act, allocated) in zip(ordered, allocations, strict=True):
        data = _scene_direction(scene, act, scene.narrative_purpose or "")
        data["duration_seconds"] = scene.duration_seconds or allocated
        data["scene_type"] = scene.scene_type if scene.scene_type in STORYBOARD_SCENE_TYPES else "product_demo"
        scenes.append(StoryboardScene.model_validate(data))

    return Storyboard(
        version="cinematic-1",
        style_preset=(style_preset or scenario.visual_style_preset or DEFAULT_STYLE_PRESET),
        character_bible=bible,
        scenes=scenes,
    )


def storyboard_issues(storyboard: Storyboard) -> list[str]:
    """Deterministic story/continuity checks; human review stays separate."""
    issues: list[str] = []
    try:
        Storyboard.model_validate(storyboard.model_dump())
    except Exception as exc:  # pydantic message is useful for the writer retry
        issues.append(f"storyboard invalid: {exc}")
    known = {spec.character_id for spec in storyboard.character_bible.characters}
    seen_actions: set[str] = set()
    for scene in storyboard.scenes:
        if not scene.narrative_purpose.strip():
            issues.append(f"{scene.scene_id}: no narrative purpose.")
        if scene.scene_type not in STORYBOARD_SCENE_TYPES:
            issues.append(f"{scene.scene_id}: unsupported scene_type {scene.scene_type!r}.")
        if not scene.continuity_constraints:
            issues.append(f"{scene.scene_id}: continuity constraints are missing.")
        for character_id in scene.characters:
            if character_id not in known:
                issues.append(f"{scene.scene_id}: unknown character {character_id!r}.")
        action_key = " ".join(scene.character_actions.lower().split())
        if action_key and action_key in seen_actions:
            issues.append(f"{scene.scene_id}: repeats another scene's action.")
        seen_actions.add(action_key)
    return issues


def character_bible_from_raw(value: Any | None) -> CharacterBible | None:
    return build_character_bible(value)


__all__ = [
    "DEFAULT_STYLE_PRESET",
    "STYLE_PRESETS",
    "StylePreset",
    "build_storyboard",
    "character_block",
    "character_bible_from_raw",
    "get_style_preset",
    "storyboard_issues",
    "style_block",
]
