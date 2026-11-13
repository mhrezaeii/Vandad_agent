"""Scenario schemas for the scene-based (non-avatar) video pipeline.

A scenario is the structured, provider-neutral description of one short
vertical video. The text-generation API produces it; every scene carries the
Persian narration plus an English visual prompt that any scene-video provider
can consume.

Duration rules keep the output social-ready: roughly 5-6 scenes per 30
seconds, and per-scene durations are always normalised so their sum matches
the requested target duration.
"""

from __future__ import annotations

import math
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Scene-count envelope. A single-scene project is allowed because the provider's
# one clip is itself a complete deliverable and needs no composition stage; the
# upper bound keeps generation cost and pacing sane.
MIN_SCENES = 1
MAX_SCENES = 12
# ~5-6 scenes per 30s is the target pacing.
SCENES_PER_30_SECONDS = 5.5
MIN_SCENE_DURATION_SECONDS = 1.0
MAX_SCENE_DURATION_SECONDS = 20.0
MIN_TARGET_DURATION_SECONDS = 5
MAX_TARGET_DURATION_SECONDS = 120


def expected_scene_count(target_duration_seconds: float) -> int:
    """Return the recommended scene count for a target duration."""
    raw = round((float(target_duration_seconds) / 30.0) * SCENES_PER_30_SECONDS)
    return max(MIN_SCENES, min(MAX_SCENES, raw))


class ScenarioRequest(BaseModel):
    """Inputs used to build the scenario-generation prompt."""

    model_config = ConfigDict(extra="forbid")

    topic: str = Field(min_length=3, max_length=2000)
    target_duration_seconds: int = Field(default=30, ge=MIN_TARGET_DURATION_SECONDS, le=MAX_TARGET_DURATION_SECONDS)
    language: str = Field(default="fa-IR", min_length=2, max_length=20)
    brand_name: str | None = Field(default=None, max_length=150)
    brand_tone: str | None = Field(default=None, max_length=1000)
    cta_text: str | None = Field(default=None, max_length=500)
    hashtags: list[str] = Field(default_factory=list, max_length=30)
    scene_count: int | None = Field(default=None, ge=MIN_SCENES, le=MAX_SCENES)
    product_context: str | None = Field(default=None, max_length=3000)
    platform_knowledge_context: str | None = Field(default=None, max_length=12000)

    @field_validator("topic")
    @classmethod
    def _strip_topic(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Topic must not be blank.")
        return stripped

    @field_validator("hashtags")
    @classmethod
    def _clean_hashtags(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for tag in value:
            normalized = tag.strip()
            if normalized and normalized not in cleaned:
                cleaned.append(normalized)
        return cleaned

    @property
    def resolved_scene_count(self) -> int:
        return self.scene_count or expected_scene_count(self.target_duration_seconds)


class SceneSpec(BaseModel):
    """One visual beat of the video."""

    model_config = ConfigDict(extra="ignore")

    index: int = Field(default=0, ge=0)
    narration: str = Field(min_length=1, max_length=2000, description="Persian narration for this scene.")
    visual_prompt: str = Field(
        min_length=1,
        max_length=2000,
        description="English visual prompt for the scene-video provider.",
    )
    duration_seconds: float = Field(gt=0, description="Scene duration in seconds.")
    on_screen_text: str | None = Field(default=None, max_length=500, description="Persian on-screen text.")
    subtitle_text: str | None = Field(default=None, max_length=2000, description="Persian subtitle text.")
    scene_type: Literal["problem", "product_demo", "b_roll", "motion_graphic", "cta"] = "b_roll"
    camera_direction: str | None = Field(default=None, max_length=1000)
    transition: Literal["cut", "dissolve", "whip", "match_cut", "fade"] = "cut"

    @field_validator("narration", "visual_prompt")
    @classmethod
    def _strip_required(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Field must not be blank.")
        return stripped

    @field_validator("on_screen_text", "subtitle_text")
    @classmethod
    def _strip_optional(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None


class Scenario(BaseModel):
    """Validated, duration-normalised scenario."""

    model_config = ConfigDict(extra="ignore")

    title: str = Field(min_length=1, max_length=500)
    opening_hook: str = Field(min_length=1, max_length=1000)
    scenes: list[SceneSpec] = Field(min_length=MIN_SCENES, max_length=MAX_SCENES)
    narration: str = Field(min_length=1, description="Full Persian narration, scenes joined in order.")
    cta: str = Field(default="", max_length=500)
    caption: str = Field(default="", max_length=2000)
    hashtags: list[str] = Field(default_factory=list, max_length=30)
    target_duration_seconds: int = Field(ge=MIN_TARGET_DURATION_SECONDS, le=MAX_TARGET_DURATION_SECONDS)
    visual_style: str | None = Field(default=None, max_length=1000)
    language: str = Field(default="fa-IR", min_length=2, max_length=20)

    @field_validator("title", "opening_hook", "narration")
    @classmethod
    def _strip_required(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Field must not be blank.")
        return stripped

    @field_validator("hashtags")
    @classmethod
    def _clean_hashtags(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for tag in value:
            normalized = tag.strip()
            if normalized and normalized not in cleaned:
                cleaned.append(normalized)
        return cleaned

    @model_validator(mode="after")
    def _check_duration(self) -> Scenario:
        total = sum(scene.duration_seconds for scene in self.scenes)
        if total <= 0:
            raise ValueError("Scenario scenes must have a positive total duration.")
        return self

    @property
    def total_duration_seconds(self) -> float:
        return round(sum(scene.duration_seconds for scene in self.scenes), 3)


def _coerce_float(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number) or number <= 0:
        return default
    return number


def _normalize_durations(scenes: list[SceneSpec], target_duration_seconds: float) -> list[SceneSpec]:
    """Scale scene durations so they sum exactly to the target duration.

    Every scene keeps at least ``MIN_SCENE_DURATION_SECONDS``; the remainder is
    distributed proportionally over the scenes that have room to grow.
    """
    count = len(scenes)
    minimum_total = MIN_SCENE_DURATION_SECONDS * count
    target = max(float(target_duration_seconds), minimum_total)

    raw_total = sum(scene.duration_seconds for scene in scenes)
    if raw_total <= 0:
        raw_total = float(count)
        weights = [1.0] * count
    else:
        weights = [scene.duration_seconds for scene in scenes]

    scaled = [max(MIN_SCENE_DURATION_SECONDS, target * weight / raw_total) for weight in weights]
    current_total = sum(scaled)
    if current_total != target:
        # Correct the rounding drift on the largest scene so the sum is exact
        # to the millisecond without pushing any scene below the minimum.
        delta = target - current_total
        adjustable = [i for i, value in enumerate(scaled) if value + delta >= MIN_SCENE_DURATION_SECONDS]
        if adjustable:
            largest = max(adjustable, key=lambda i: scaled[i])
            scaled[largest] = max(MIN_SCENE_DURATION_SECONDS, scaled[largest] + delta)

    normalized: list[SceneSpec] = []
    for index, (scene, duration) in enumerate(zip(scenes, scaled, strict=True)):
        normalized.append(
            scene.model_copy(
                update={
                    "index": index,
                    "duration_seconds": round(duration, 3),
                }
            )
        )
    return normalized


def normalize_scenario(raw: dict[str, Any], request: ScenarioRequest) -> Scenario:
    """Validate and normalise a raw scenario payload from the LLM.

    Raises ``ValueError`` (or pydantic ``ValidationError``) when the payload is
    unusable; the service maps that to a clear generation error.
    """
    if not isinstance(raw, dict):
        raise ValueError("Scenario payload must be a JSON object.")

    raw_scenes = raw.get("scenes")
    if not isinstance(raw_scenes, list) or not raw_scenes:
        raise ValueError("Scenario payload must contain a non-empty 'scenes' array.")

    target_duration = float(request.target_duration_seconds)
    fallback_duration = max(MIN_SCENE_DURATION_SECONDS, target_duration / len(raw_scenes))

    scenes: list[SceneSpec] = []
    for position, item in enumerate(raw_scenes):
        if not isinstance(item, dict):
            raise ValueError("Every scene must be a JSON object.")
        narration = item.get("narration") or item.get("voiceover") or item.get("text")
        visual_prompt = (
            item.get("visual_prompt")
            or item.get("visual")
            or item.get("prompt")
            or item.get("image_prompt")
        )
        if not isinstance(narration, str) or not narration.strip():
            raise ValueError(f"Scene {position} has no narration text.")
        if not isinstance(visual_prompt, str) or not visual_prompt.strip():
            raise ValueError(f"Scene {position} has no visual prompt.")
        duration = _coerce_float(item.get("duration_seconds") or item.get("duration"), fallback_duration)
        duration = max(MIN_SCENE_DURATION_SECONDS, min(MAX_SCENE_DURATION_SECONDS, duration))
        scenes.append(
            SceneSpec(
                index=position,
                narration=narration,
                visual_prompt=visual_prompt,
                duration_seconds=duration,
                on_screen_text=item.get("on_screen_text") or item.get("display_text"),
                subtitle_text=item.get("subtitle_text") or item.get("subtitle"),
                scene_type=item.get("scene_type", "b_roll"),
                camera_direction=item.get("camera_direction"),
                transition=item.get("transition", "cut"),
            )
        )

    if len(scenes) < MIN_SCENES:
        raise ValueError(
            f"Scenario must contain at least {MIN_SCENES} scenes (received {len(scenes)})."
        )
    if len(scenes) > MAX_SCENES:
        scenes = scenes[:MAX_SCENES]

    scenes = _normalize_durations(scenes, target_duration)

    narration = raw.get("narration")
    if not isinstance(narration, str) or not narration.strip():
        narration = " ".join(scene.narration for scene in scenes)

    hashtags = raw.get("hashtags") or request.hashtags
    if not isinstance(hashtags, list):
        hashtags = request.hashtags

    title = raw.get("title")
    if not isinstance(title, str) or not title.strip():
        title = request.topic[:200]

    opening_hook = raw.get("opening_hook") or raw.get("hook")
    if not isinstance(opening_hook, str) or not opening_hook.strip():
        opening_hook = scenes[0].narration

    cta = raw.get("cta") or raw.get("call_to_action") or request.cta_text or ""
    if not isinstance(cta, str):
        cta = ""

    caption = raw.get("caption")
    if not isinstance(caption, str):
        caption = ""

    visual_style = raw.get("visual_style")
    if not isinstance(visual_style, str):
        visual_style = None

    return Scenario(
        title=title,
        opening_hook=opening_hook,
        scenes=scenes,
        narration=narration,
        cta=cta,
        caption=caption,
        hashtags=hashtags,
        target_duration_seconds=request.target_duration_seconds,
        visual_style=visual_style,
        language=request.language,
    )
