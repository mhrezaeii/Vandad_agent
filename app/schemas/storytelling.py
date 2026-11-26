"""Provider-neutral storyboard and character-bible schemas.

These models describe the *creative* layer of a video before any provider
prompt is composed. They are deliberately independent of KIE/Pydantic video
adapters so the same storyboard can be reused by any scene-video provider.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ActName = Literal["problem", "escalation", "solution", "transformation", "brand_cta"]

# Mid-points of the permitted act ranges (15-20 / 10-15 / 15-20 / 25-30 / 10-15).
# The values are guidance for the writer, not a hard timing contract.
ACT_DURATION_SHARE: dict[str, float] = {
    "problem": 0.18,
    "escalation": 0.12,
    "solution": 0.18,
    "transformation": 0.28,
    "brand_cta": 0.14,
}

ACT_PURPOSE: dict[str, str] = {
    "problem": "Make a recognisable real-world problem understandable without narration alone.",
    "escalation": "Increase tension or add a humorous complication without repeating the setup.",
    "solution": "Introduce the product naturally and connect it visually to the problem.",
    "transformation": "Show the product changing the situation with concrete, believable results.",
    "brand_cta": "Close with an intentional, professionally designed brand moment and one CTA.",
}

SceneKind = Literal[
    # legacy values kept for backward compatibility with stored scenarios
    "problem", "product_demo", "b_roll", "motion_graphic", "cta",
    # directed storyboard values
    "establishing", "character_introduction", "problem_demonstration", "reaction",
    "product_reveal", "transformation", "hero", "brand_ending",
]

STORYBOARD_SCENE_TYPES: tuple[str, ...] = (
    "establishing", "character_introduction", "problem_demonstration", "reaction",
    "product_reveal", "product_demo", "transformation", "hero", "brand_ending",
)


def normalize_key(value: str) -> str:
    """Diacritic- and punctuation-insensitive key for identity comparisons."""
    normalized = unicodedata.normalize("NFKC", value or "").translate(
        str.maketrans({"ي": "ی", "ك": "ک"})
    )
    normalized = re.sub(r"[\u064B-\u065F\u0670\u06D6-\u06ED]", "", normalized)
    return re.sub(r"[^0-9A-Za-z\u0600-\u06FF]+", "", normalized).casefold()


def allocate_act_durations(total_seconds: float, scene_count: int) -> list[tuple[str, float]]:
    """Assign an act and a target duration to every scene.

    Five or more scenes cover all five acts. Shorter videos still receive a
    sensible subset, weighted by the same act shares, and durations always sum
    to ``total_seconds`` (to two decimals).
    """
    if scene_count < 1:
        return []
    total = max(float(total_seconds), float(scene_count))
    acts = list(ACT_DURATION_SHARE)

    if scene_count >= len(acts):
        assignments = acts[:]
        while len(assignments) < scene_count:
            # Extra beats go where the reference story spends its time.
            assignments.append(
                max(acts, key=lambda act: ACT_DURATION_SHARE[act] / assignments.count(act))
            )
        assignments.sort(key=acts.index)
    else:
        # Short films still progress through the story: first beat is the
        # problem, last beat is the brand ending, middle beats are spread
        # across the intervening acts.
        last = len(acts) - 1
        assignments = [
            acts[min(last, round(index * last / max(1, scene_count - 1)))]
            for index in range(scene_count)
        ]

    counts = {act: assignments.count(act) for act in acts}
    weights = [
        ACT_DURATION_SHARE[act] / max(1, counts[act]) for act in assignments
    ]
    weight_total = sum(weights) or 1.0
    durations = [round(total * weight / weight_total, 2) for weight in weights]
    drift = round(total - sum(durations), 2)
    if durations and drift:
        largest = max(range(len(durations)), key=lambda i: durations[i])
        durations[largest] = round(durations[largest] + drift, 2)
    return list(zip(assignments, durations, strict=True))


class CharacterSpec(BaseModel):
    """One reusable character identity in the Character Bible."""

    model_config = ConfigDict(extra="ignore")

    character_id: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=150)
    age_range: str | None = Field(default=None, max_length=100)
    face_shape: str | None = Field(default=None, max_length=300)
    hair_style: str | None = Field(default=None, max_length=300)
    hair_color: str | None = Field(default=None, max_length=100)
    eye_characteristics: str | None = Field(default=None, max_length=300)
    body_proportions: str | None = Field(default=None, max_length=300)
    clothing: str | None = Field(default=None, max_length=300)
    accessories: list[str] = Field(default_factory=list, max_length=12)
    personality: str | None = Field(default=None, max_length=300)
    emotional_range: str | None = Field(default=None, max_length=300)
    animation_style: str | None = Field(default=None, max_length=300)
    voice_identity: str | None = Field(default=None, max_length=300)
    is_robot: bool = False
    robot_design_lock: str | None = Field(default=None, max_length=600)
    reference_image_key: str | None = Field(default=None, max_length=300)

    @field_validator("accessories")
    @classmethod
    def _clean_accessories(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for item in value:
            item = str(item).strip()
            if item and item not in cleaned:
                cleaned.append(item)
        return cleaned

    def identity_description(self) -> str:
        """Stable English identity sentence used in every scene prompt."""
        parts = [self.name]
        for value in (
            self.age_range, self.face_shape, self.hair_style, self.hair_color,
            self.eye_characteristics, self.body_proportions, self.clothing,
            ", ".join(self.accessories),
        ):
            if value:
                parts.append(str(value))
        if self.is_robot and self.robot_design_lock:
            parts.append(self.robot_design_lock)
        return "; ".join(parts)


class CharacterBible(BaseModel):
    """Reusable identity contract for every recurring character in a film."""

    model_config = ConfigDict(extra="ignore")

    style_preset: str = Field(default="cinematic_3d_storytelling", max_length=100)
    characters: list[CharacterSpec] = Field(default_factory=list, max_length=8)
    relationships: list[str] = Field(default_factory=list, max_length=20)
    continuity_rules: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("relationships", "continuity_rules")
    @classmethod
    def _clean_lines(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for item in value:
            item = str(item).strip()
            if item and item not in cleaned:
                cleaned.append(item)
        return cleaned

    @model_validator(mode="after")
    def _unique_identities(self) -> CharacterBible:
        ids = [normalize_key(spec.character_id) for spec in self.characters]
        names = [normalize_key(spec.name) for spec in self.characters]
        if len(set(ids)) != len(ids) or len(set(names)) != len(names):
            raise ValueError("character_id and name must be unique in a Character Bible.")
        for spec in self.characters:
            if spec.is_robot and not spec.robot_design_lock:
                raise ValueError("robot characters require a robot_design_lock description.")
        return self

    def by_id(self, character_id: str) -> CharacterSpec | None:
        key = normalize_key(character_id)
        return next(
            (spec for spec in self.characters if normalize_key(spec.character_id) == key),
            None,
        )


class StoryboardScene(BaseModel):
    """A single directed storyboard beat, independent of provider prompts."""

    model_config = ConfigDict(extra="ignore")

    scene_id: str = Field(min_length=1, max_length=100)
    act: ActName
    narrative_purpose: str = Field(min_length=1, max_length=500)
    scene_type: SceneKind = "b_roll"
    duration_seconds: float = Field(gt=0)
    characters: list[str] = Field(default_factory=list, max_length=8)
    character_actions: str = Field(default="", max_length=1000)
    character_emotions: str = Field(default="", max_length=500)
    environment: str = Field(default="", max_length=1000)
    camera_shot: str = Field(default="", max_length=500)
    camera_angle: str = Field(default="", max_length=500)
    camera_movement: str = Field(default="", max_length=500)
    lighting: str = Field(default="", max_length=500)
    visual_composition: str = Field(default="", max_length=1000)
    transition_in: str = Field(default="cut", max_length=100)
    transition_out: str = Field(default="cut", max_length=100)
    narration: str = Field(min_length=1, max_length=2000)
    pronunciation_text: str = Field(min_length=1, max_length=2000)
    sound_design: str = Field(default="", max_length=500)
    visual_prompt: str = Field(min_length=1, max_length=2000)
    continuity_constraints: list[str] = Field(default_factory=list, max_length=12)

    @model_validator(mode="after")
    def _required_direction(self) -> StoryboardScene:
        if not self.narrative_purpose.strip():
            raise ValueError("Every storyboard scene needs a narrative_purpose.")
        if not self.visual_prompt.strip():
            raise ValueError("Every storyboard scene needs a visual_prompt.")
        return self


class Storyboard(BaseModel):
    """Validated five-act storyboard produced before any paid generation."""

    model_config = ConfigDict(extra="ignore")

    version: str = Field(default="cinematic-1", max_length=50)
    style_preset: str = Field(default="cinematic_3d_storytelling", max_length=100)
    character_bible: CharacterBible = Field(default_factory=CharacterBible)
    scenes: list[StoryboardScene] = Field(min_length=1, max_length=12)

    @model_validator(mode="after")
    def _validate_storyboard(self) -> Storyboard:
        ids = [normalize_key(scene.scene_id) for scene in self.scenes]
        if len(set(ids)) != len(ids):
            raise ValueError("storyboard scene_id values must be unique.")
        purposes = [normalize_key(scene.narrative_purpose) for scene in self.scenes]
        if any(purpose and purposes.count(purpose) > 1 for purpose in purposes):
            raise ValueError("No two storyboard scenes may share a narrative_purpose.")
        known_characters = {
            normalize_key(spec.character_id) for spec in self.character_bible.characters
        }
        for scene in self.scenes:
            for character_id in scene.characters:
                if normalize_key(character_id) not in known_characters:
                    raise ValueError(
                        f"Scene {scene.scene_id} uses unknown character {character_id!r}."
                    )
        if len(self.scenes) >= len(ACT_DURATION_SHARE):
            missing = set(ACT_DURATION_SHARE) - {scene.act for scene in self.scenes}
            if missing:
                raise ValueError(f"Storyboard is missing acts: {', '.join(sorted(missing))}.")
        return self

    @property
    def total_duration_seconds(self) -> float:
        return round(sum(scene.duration_seconds for scene in self.scenes), 2)


def build_character_bible(value: Any | None) -> CharacterBible | None:
    """Leniently parse an LLM-produced bible; unusable payloads are ignored."""
    if value is None or isinstance(value, CharacterBible):
        return value
    if not isinstance(value, dict):
        return None
    try:
        return CharacterBible.model_validate(value)
    except Exception:
        return None


__all__ = [
    "ACT_DURATION_SHARE",
    "ACT_PURPOSE",
    "STORYBOARD_SCENE_TYPES",
    "ActName",
    "CharacterBible",
    "CharacterSpec",
    "SceneKind",
    "Storyboard",
    "StoryboardScene",
    "allocate_act_durations",
    "build_character_bible",
    "normalize_key",
]
