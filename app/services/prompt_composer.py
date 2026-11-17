"""Single-shot prompt composition: one complete scenario -> one provider prompt.

The multi-scene pipeline submits one paid request per storyboard beat. The
single-shot mode instead sends exactly ONE generation request whose prompt
describes the whole video: opening hook, every storyboard beat in order, and
the closing call to action.

Composition rules:

- Persian narration is embedded verbatim (never paraphrased, translated or
  truncated) and the model is instructed to speak exactly that narration.
- On-screen text and subtitle guidance stay Persian and right-to-left.
- Visual/camera directions may remain English for better visual adherence.
- Character-identity instructions keep one consistent character throughout.
- The composer never silently truncates content: if the composed prompt does
  not fit the provider limit, it fails with :class:`PromptCompositionError`.
"""

from __future__ import annotations

from app.schemas.scenario import Scenario


class PromptCompositionError(Exception):
    """A single-shot prompt could not be composed within the provider limit."""

    def __init__(self, message: str, *, code: str = "prompt_composition_failed") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


_PERSIAN_DELIVERY_RULES = (
    "SPEECH (mandatory): Speak fluent Iranian Persian (Farsi) with natural "
    "Iranian pronunciation and intonation. Do NOT speak English. Do NOT "
    "translate, paraphrase, shorten or omit the Persian pronunciation_text "
    "provided below; obey its diacritics and speak each line exactly as written, "
    "in order, synchronised with "
    "the matching visual beat."
)

_ON_SCREEN_RULES = (
    "ON-SCREEN TEXT (mandatory): Render the provided Persian on-screen text "
    "exactly as written, right-to-left, using a clean bold Persian font. "
    "Do not add any other readable text, labels, captions or watermarks."
)

_CAMERA_RULES = (
    "TIMING (mandatory): Every beat below has an explicit duration; pace the "
    "cuts so the beats play in order for the stated durations and the total "
    "matches the requested video length."
)


def compose_single_shot_prompt(
    scenario: Scenario,
    *,
    character_description: str | None = None,
    has_reference_image: bool = False,
    max_characters: int = 5000,
) -> str:
    """Compose the complete scenario into one image-to-video generation prompt.

    ``character_description`` should describe the person shown in the attached
    reference image so the model keeps one consistent identity throughout.
    Raises :class:`PromptCompositionError` when the required content cannot
    fit ``max_characters``; narration is never truncated.
    """
    if max_characters <= 0:
        raise PromptCompositionError(
            "The single-shot prompt limit must be a positive number of characters.",
            code="prompt_invalid_limit",
        )

    lines: list[str] = []
    lines.append(
        "GOAL: Generate ONE continuous vertical brand film as a single video. "
        "The storyboard below describes the beginning, middle and ending; play "
        "all beats in order with coherent camera work, consistent lighting and "
        "smooth transitions."
    )

    character_block = (character_description or "").strip()
    if has_reference_image or character_block:
        lines.append(
            "REFERENCE IMAGE (immutable source, mandatory): Use the exact attached "
            "image as the sole character source. Do not edit, retouch, restyle, redraw, "
            "replace, beautify, crop or otherwise alter the source image. Animate from "
            "this reference only; preserve the same face, age, hair, clothing, accessories, "
            "proportions and identity in every generated frame. Do not invent a substitute."
        )
        if character_block:
            lines.append(
                f"CHARACTER DESCRIPTION (operator supplied): {character_block}. "
                "This description supplements the image and never overrides it."
            )

    if scenario.visual_style:
        lines.append(f"VISUAL STYLE: {scenario.visual_style.strip()}")

    lines.append(_CAMERA_RULES)
    lines.append(_PERSIAN_DELIVERY_RULES)
    lines.append(_ON_SCREEN_RULES)

    lines.append("STORYBOARD (play beats in this exact order):")
    for scene in sorted(scenario.scenes, key=lambda item: item.index):
        beat = [f"Beat {scene.index + 1} ({_format_seconds(scene.duration_seconds)}):"]
        if scene.camera_direction:
            beat.append(f"CAMERA: {scene.camera_direction.strip()}")
        beat.append(f"VISUAL: {scene.visual_prompt.strip()}")
        lines.append(" ".join(beat))
        lines.append(
            "NARRATION (speak exactly, in Persian, obeying diacritics): "
            f"{(scene.pronunciation_text or scene.narration).strip()}"
        )
        if scene.on_screen_text:
            lines.append(f"ON-SCREEN TEXT (Persian, RTL): {scene.on_screen_text.strip()}")

    if scenario.cta.strip():
        lines.append(
            "ENDING CTA (speak exactly, in Persian, obeying diacritics): "
            f"{(scenario.cta_pronunciation_text or scenario.cta).strip()}"
        )

    prompt = "\n".join(lines)
    if len(prompt) > max_characters:
        raise PromptCompositionError(
            "The single-shot prompt is "
            f"{len(prompt)} characters but the provider limit is {max_characters}. "
            "Shorten the scenario (fewer beats or tighter narration) instead of "
            "truncating it; the narration is never cut silently.",
            code="prompt_too_long",
        )
    return prompt


def _format_seconds(seconds: float) -> str:
    if float(seconds).is_integer():
        return f"{int(seconds)}s"
    return f"{round(float(seconds), 1)}s"


__all__ = ["PromptCompositionError", "compose_single_shot_prompt"]
