"""Single-shot prompt composition: one complete scenario -> one provider prompt.

The multi-scene pipeline submits one paid request per storyboard beat. The
single-shot mode instead sends exactly ONE generation request whose prompt
describes the whole video: opening hook, every storyboard beat in order, and
the closing call to action.

Composition rules:

- Persian narration is embedded verbatim (never paraphrased, translated or
  truncated) and the model is instructed to speak it at the measured pace and
  exactly once, without improvising extra lines.
- The closing CTA stays verbatim and is appended only once, after the last beat.
- No captions, subtitles or other text may be rendered anywhere in the video.
- Visual/camera directions may remain English for better visual adherence.
- Character-identity instructions keep one consistent character throughout.
- If the full prompt does not fit, the composer deterministically removes only
  optional guidance (shorter rules, then camera directions, then visual text).
  Narration and CTA are never shortened, reordered or omitted.
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
    "smooth transitions. Stop immediately after the final beat and the single "
    "ending CTA; do not add a new scene, sentence or repeated line afterwards."
)
_GOAL_COMPACT = (
    "GOAL: Generate ONE continuous vertical brand film. Play all storyboard "
    "beats in order with coherent camera work, lighting and smooth transitions. "
    "Stop after the final beat; no continuation after the ending CTA."
)
_GOAL_MINIMAL = (
    "GOAL: ONE continuous vertical brand film; all beats in order; stop after "
    "the final beat, no continuation."
)

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
    "SPEECH (mandatory): Use a natural Iranian Persian (Farsi) voice. Read ONLY the "
    "NARRATION and ENDING CTA lines, word for word, exactly as written and in order, "
    "synchronised with the matching visual beat and with no added or repeated words. "
    "Do NOT slow down artificially, rush or compress the narration to match the cuts. "
    "If a line ends early, hold the frame silently instead of speaking again. Do NOT "
    "speak English except brand names or acronyms that appear verbatim in the "
    "provided text. Stop speaking when the final line ends: add no words, no extra "
    "sentence and no repeated CTA after it. Do NOT translate, paraphrase, shorten or "
    "omit any spoken line, and do not add any other speech."
)
_SPEECH_COMPACT = (
    "SPEECH (mandatory): Natural Iranian Persian (Farsi). Read ONLY the NARRATION/CTA "
    "lines below, word for word, exactly as written and in order. Do NOT slow down, "
    "rush, translate, paraphrase, shorten, omit or repeat. If the line ends early, "
    "hold the frame silently; add no other speech."
)
_SPEECH_MINIMAL = (
    "SPEECH: Natural Iranian Persian (Farsi). Read ONLY NARRATION/CTA lines below, "
    "word for word, exactly as written and in order. No slowing, rushing, "
    "translating, paraphrasing, shortening, omitting or repeating; hold the frame "
    "silently if early, add no extra speech."
)

_ON_SCREEN_FULL = (
    "NO ON-SCREEN TEXT (mandatory): Do not render any text, caption, subtitle, "
    "label, watermark, logo or generated lettering in any frame. Keep the frame "
    "text-free even when the product UI is visible."
)
_ON_SCREEN_COMPACT = (
    "NO ON-SCREEN TEXT: Render no text, captions, subtitles, labels, watermarks "
    "or logos in any frame."
)
_ON_SCREEN_MINIMAL = "NO TEXT: no captions, subtitles, labels, watermarks or logos."

_TIMING_FULL = (
    "TIMING (mandatory): Every beat below has an explicit duration; pace the "
    "cuts so the beats play in order for the stated durations and the total "
    "matches the requested video length. Complete, verbatim speech takes priority "
    "over cut timing: hold a frame if needed, never speed up or add words."
)
_TIMING_COMPACT = (
    "TIMING (mandatory): Beats play in order at the stated durations; the total "
    "matches the video length. Never shorten or extend speech to fit the timing."
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
                "NARRATION (speak exactly, at the measured pace, in Persian):"
            )
            lines.append(f"{label} {spoken}")

    cta = _spoken_cta(scenario)
    if cta:
        label = (
            "CTA:"
            if layout.compact_labels
            else "ENDING CTA (speak exactly, at the measured pace, in Persian):"
        )
        lines.append(f"{label} {cta}")

    return "\n".join(lines)


_DIACRITICS_RE = re.compile(r"[\u064B-\u065F\u0670\u06D6-\u06ED]")


def _spoken_text(scene: SceneSpec) -> str:
    """The pronunciation_text is authoritative for the provider.

    This is the dedicated pronunciation-ready field, which preserves exactly
    the same words and word order as narration with the selective diacritic
    rule applied. Never use scene.narration directly for speech output.
    """
    return scene.pronunciation_text.strip()


def _spoken_key(value: str) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFKC", value or "").translate(
        str.maketrans({"ي": "ی", "ك": "ک"})
    )
    return tuple(re.findall(r"[\w]+", _DIACRITICS_RE.sub("", normalized), flags=re.UNICODE))


def _spoken_cta(scenario: Scenario) -> str:
    """Append the CTA once, using cta_pronunciation_text.

    The cta_pronunciation_text field is the authoritative spoken CTA text.
    """
    cta = scenario.cta_pronunciation_text.strip()
    if not cta or not scenario.scenes:
        return ""
    last_scene = max(scenario.scenes, key=lambda item: item.index)
    cta_key = _spoken_key(cta)
    last_key = _spoken_key(last_scene.pronunciation_text)
    if not cta_key or not last_key:
        return cta
    if cta_key == last_key:
        return ""
    # A partial CTA at the end would otherwise be spoken twice.
    width = len(cta_key)
    if width <= len(last_key):
        for index in range(len(last_key) - width + 1):
            if last_key[index : index + width] == cta_key:
                return ""
    return cta


def _format_seconds(seconds: float) -> str:
    if float(seconds).is_integer():
        return f"{int(seconds)}s"
    return f"{round(float(seconds), 1)}s"


__all__ = ["PromptCompositionError", "compose_single_shot_prompt"]
