"""Single-shot prompt composition: one complete scenario -> one provider prompt.

The multi-scene pipeline submits one paid request per storyboard beat. The
single-shot mode instead sends exactly ONE generation request whose prompt
describes the whole video: opening hook, every storyboard beat in order, and
the closing call to action.

Composition rules:

- Persian narration is embedded verbatim (never paraphrased, translated or
  truncated) and the model is instructed to speak it slowly and exactly.
- On-screen text and CTA stay verbatim; on-screen text is visual only and is
  never read aloud by the voice-over.
- Visual/camera directions may remain English for better visual adherence.
- Character-identity instructions keep one consistent character throughout.
- If the full prompt does not fit, the composer deterministically removes only
  optional guidance (shorter rules, then camera directions, then visual text).
  Narration, on-screen text and CTA are never shortened, reordered or omitted.
  It fails with :class:`PromptCompositionError` only when even that tightest
  prompt cannot fit.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from app.schemas.scenario import Scenario, SceneSpec


class PromptCompositionError(Exception):
    """A single-shot prompt could not be composed within the provider limit."""

    def __init__(self, message: str, *, code: str = "prompt_composition_failed") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


# Full delivery rules. The exact substrings checked by the regression tests are
# intentional: they encode the product promise that narration is never changed.
_GOAL_FULL = (
    "GOAL: Generate ONE continuous vertical brand film as a single video. "
    "The storyboard below describes the beginning, middle and ending; play "
    "all beats in order with coherent camera work, consistent lighting and "
    "smooth transitions."
)
_GOAL_COMPACT = (
    "GOAL: Generate ONE continuous vertical brand film. Play all storyboard "
    "beats in order with coherent camera work, lighting and smooth transitions."
)
_GOAL_MINIMAL = "GOAL: ONE continuous vertical brand film; all beats in order."

_REFERENCE_FULL = (
    "REFERENCE IMAGE (immutable source, mandatory): Use the exact attached "
    "image as the sole character source. Do not edit, retouch, restyle, redraw, "
    "replace, beautify, crop or otherwise alter the source image. Animate from "
    "this reference only; preserve the same face, age, hair, clothing, accessories, "
    "proportions and identity in every generated frame. Do not invent a substitute. "
    "CHARACTER CONSISTENCY (mandatory): The reference image is authoritative. "
    "Never introduce a different person or replace the supplied character."
)
_REFERENCE_COMPACT = (
    "REFERENCE IMAGE (mandatory): Use the exact attached image as the sole "
    "character source. Preserve the same face, age, hair, clothing, accessories, "
    "proportions and identity in every frame. Do not edit or replace it. "
    "CHARACTER CONSISTENCY: the image is authoritative; never introduce a different person."
)
_REFERENCE_MINIMAL = (
    "REFERENCE IMAGE: exact attached image only; keep the same identity in every "
    "frame; no substitute."
)

_CHARACTER_FULL = (
    "CHARACTER DESCRIPTION (operator supplied): {description}. This description "
    "supplements the image and never overrides it."
)
_CHARACTER_COMPACT = (
    "CHARACTER: {description} (supplements the image; never overrides it)."
)

_SPEECH_FULL = (
    "SPEECH (mandatory): Use a calm, unhurried Iranian Persian (Farsi) voice at a "
    "deliberately slow, measured pace (about 90 words per minute), clearly slower "
    "than normal conversational speed, with gentle pauses between sentences and "
    "even intonation. Do NOT rush, speed up or compress the narration to match the "
    "cuts; hold a frame or extend a beat instead. Do NOT speak English except brand "
    "names or acronyms that appear verbatim in the provided text. Read ONLY the "
    "NARRATION and ENDING CTA lines below, word for word, exactly as written and in "
    "order, synchronised with the matching visual beat and with no added or repeated "
    "words. Do NOT read the ON-SCREEN TEXT lines aloud; add no words and repeat "
    "nothing. Do NOT translate, paraphrase, shorten or omit any spoken line, and do "
    "not add any other speech."
)
_SPEECH_COMPACT = (
    "SPEECH (mandatory): Calm Iranian Persian (Farsi), deliberately slow (about 90 "
    "words per minute) with gentle pauses. Never rush or speed up for the cuts; "
    "hold frames instead. Read ONLY the NARRATION/CTA lines below, word for word, "
    "exactly as written and in order. Do NOT translate, paraphrase, shorten, omit "
    "or repeat; never read ON-SCREEN TEXT aloud; add no other speech."
)
_SPEECH_MINIMAL = (
    "SPEECH: Slow, calm Iranian Persian (Farsi, about 90 words per minute). Never "
    "rush for cuts. Read ONLY NARRATION/CTA lines below, word for word, exactly as "
    "written and in order. Do NOT translate, paraphrase, shorten, omit or repeat; "
    "never read ON-SCREEN TEXT aloud; add no other speech."
)

_ON_SCREEN_FULL = (
    "ON-SCREEN TEXT (mandatory): Render each provided Persian on-screen text "
    "exactly as written, right-to-left, using a clean bold Persian font. "
    "Do not add any other readable text, labels, captions or watermarks. "
    "On-screen text is visual only and is never spoken."
)
_ON_SCREEN_COMPACT = (
    "ON-SCREEN TEXT: Render the Persian text exactly, right-to-left, in a clean "
    "bold font. Add no other readable text or watermark; never speak it."
)
_ON_SCREEN_MINIMAL = (
    "ON-SCREEN: Persian text exactly, right-to-left; no other readable text; never spoken."
)

_TIMING_FULL = (
    "TIMING (mandatory): Every beat below has an explicit duration; pace the "
    "cuts so the beats play in order for the stated durations and the total "
    "matches the requested video length. Slow, exact speech takes priority over "
    "cut timing; hold frames rather than speeding up the voice."
)
_TIMING_COMPACT = (
    "TIMING (mandatory): Beats play in order at the stated durations; the total "
    "matches the video length. Never shorten speech to fit the timing."
)
_TIMING_MINIMAL = "TIMING: Beats in order at stated durations; total matches video length."


@dataclass(frozen=True)
class _RuleSet:
    goal: str
    reference: str
    character: str
    speech: str
    on_screen: str
    timing: str


_RULESETS = {
    "full": _RuleSet(
        goal=_GOAL_FULL,
        reference=_REFERENCE_FULL,
        character=_CHARACTER_FULL,
        speech=_SPEECH_FULL,
        on_screen=_ON_SCREEN_FULL,
        timing=_TIMING_FULL,
    ),
    "compact": _RuleSet(
        goal=_GOAL_COMPACT,
        reference=_REFERENCE_COMPACT,
        character=_CHARACTER_COMPACT,
        speech=_SPEECH_COMPACT,
        on_screen=_ON_SCREEN_COMPACT,
        timing=_TIMING_COMPACT,
    ),
    "minimal": _RuleSet(
        goal=_GOAL_MINIMAL,
        reference=_REFERENCE_MINIMAL,
        character=_CHARACTER_COMPACT,
        speech=_SPEECH_MINIMAL,
        on_screen=_ON_SCREEN_MINIMAL,
        timing=_TIMING_MINIMAL,
    ),
}


@dataclass(frozen=True)
class _Layout:
    """One deterministic compaction level, from richest to tightest."""

    rules: str
    compact_labels: bool
    include_camera: bool
    include_visual: bool
    include_style: bool


# Each level preserves every spoken/written line. Only optional guidance is
# removed, and always in the same order so a retry produces the same prompt.
_LAYOUTS: tuple[_Layout, ...] = (
    _Layout("full", compact_labels=False, include_camera=True, include_visual=True, include_style=True),
    _Layout("compact", compact_labels=False, include_camera=True, include_visual=True, include_style=True),
    _Layout("compact", compact_labels=True, include_camera=True, include_visual=True, include_style=True),
    _Layout("compact", compact_labels=True, include_camera=False, include_visual=True, include_style=True),
    _Layout("minimal", compact_labels=True, include_camera=False, include_visual=True, include_style=True),
    _Layout("minimal", compact_labels=True, include_camera=False, include_visual=True, include_style=False),
    _Layout("minimal", compact_labels=True, include_camera=False, include_visual=False, include_style=False),
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
    Raises :class:`PromptCompositionError` when even the tightest layout that
    preserves all narration/on-screen/CTA text cannot fit ``max_characters``.
    """
    if max_characters <= 0:
        raise PromptCompositionError(
            "The single-shot prompt limit must be a positive number of characters.",
            code="prompt_invalid_limit",
        )

    tightest_prompt = ""
    for layout in _LAYOUTS:
        prompt = _build_prompt(
            scenario,
            layout=layout,
            character_description=character_description,
            has_reference_image=has_reference_image,
        )
        tightest_prompt = prompt
        if len(prompt) <= max_characters:
            return prompt

    raise PromptCompositionError(
        "The complete scenario cannot fit the provider limit of "
        f"{max_characters} characters even after deterministic compaction "
        f"(tightest prompt: {len(tightest_prompt)} characters). Shorten the "
        "scenario (fewer beats or tighter narration) instead of truncating it; "
        "the narration is never cut silently.",
        code="prompt_too_long",
    )


def _build_prompt(
    scenario: Scenario,
    *,
    layout: _Layout,
    character_description: str | None,
    has_reference_image: bool,
) -> str:
    rules = _RULESETS[layout.rules]
    lines = [rules.goal]

    character_block = (character_description or "").strip()
    if has_reference_image or character_block:
        lines.append(rules.reference)
        if character_block:
            lines.append(rules.character.format(description=character_block))

    if layout.include_style and scenario.visual_style:
        lines.append(f"VISUAL STYLE: {scenario.visual_style.strip()}")

    lines.append(rules.timing)
    lines.append(rules.speech)
    lines.append(rules.on_screen)
    lines.append("STORYBOARD (play beats in this exact order):")

    for scene in sorted(scenario.scenes, key=lambda item: item.index):
        beat = [f"Beat {scene.index + 1} ({_format_seconds(scene.duration_seconds)}):"]
        if layout.include_camera and scene.camera_direction:
            beat.append(f"CAMERA: {scene.camera_direction.strip()}")
        if layout.include_visual:
            beat.append(f"VISUAL: {scene.visual_prompt.strip()}")
        lines.append(" ".join(beat))

        spoken = _spoken_text(scene)
        if spoken:
            label = "NARRATION:" if layout.compact_labels else (
                "NARRATION (speak exactly, slowly, in Persian):"
            )
            lines.append(f"{label} {spoken}")

        if scene.on_screen_text:
            label = "ON-SCREEN:" if layout.compact_labels else "ON-SCREEN TEXT (Persian, RTL):"
            lines.append(f"{label} {scene.on_screen_text.strip()}")

    cta = _spoken_cta(scenario)
    if cta:
        label = "CTA:" if layout.compact_labels else "ENDING CTA (speak exactly, slowly, in Persian):"
        lines.append(f"{label} {cta}")

    return "\n".join(lines)


_DIACRITICS_RE = re.compile(r"[\u064B-\u065F\u0670\u06D6-\u06ED]")


def _spoken_text(scene: SceneSpec) -> str:
    """The fully diacritized narration is authoritative for the video provider.

    The scenario prompt now writes every Persian spoken word with diacritics so
    the video model reads the intended vowels instead of guessing them. The
    optional ``pronunciation_text`` field is never sent to the provider.
    """
    return scene.narration.strip()


def _spoken_key(value: str) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFKC", value or "").translate(
        str.maketrans({"ي": "ی", "ك": "ک"})
    )
    return tuple(re.findall(r"[\w]+", _DIACRITICS_RE.sub("", normalized), flags=re.UNICODE))


def _spoken_cta(scenario: Scenario) -> str:
    """Return the closing CTA, unless the last beat already speaks it verbatim."""
    cta = scenario.cta.strip()
    if not cta or not scenario.scenes:
        return ""
    last_scene = max(scenario.scenes, key=lambda item: item.index)
    cta_key = _spoken_key(cta)
    if cta_key and cta_key == _spoken_key(last_scene.narration):
        return ""
    return cta


def _format_seconds(seconds: float) -> str:
    if float(seconds).is_integer():
        return f"{int(seconds)}s"
    return f"{round(float(seconds), 1)}s"


__all__ = ["PromptCompositionError", "compose_single_shot_prompt"]
