from app.services.scene_novelty import (
    extract_visual_signatures,
    is_duplicate_visual,
    normalize_visual_signature,
    visual_fingerprint,
)


def test_visual_fingerprint_normalizes_whitespace_and_punctuation():
    left = "A cinematic close-up, showing the product dashboard."
    right = "A cinematic close-up showing the product dashboard"
    assert visual_fingerprint(left) == visual_fingerprint(right)


def test_visual_duplicate_detector_catches_exact_and_near_duplicates():
    original = "A close-up cinematic shot of an office dashboard with a slow camera push-in and blue lighting"
    near = "A close up cinematic shot of an office dashboard with a slow camera push in and blue lighting"
    distinct = "Wide aerial view of a coastal city at sunrise with moving traffic and warm orange light"
    assert is_duplicate_visual(original, original)
    assert is_duplicate_visual(original, near)
    assert not is_duplicate_visual(original, distinct)


def test_extract_visual_signatures_supports_multi_and_single_shot_prompts():
    prompt = "VISUAL PROMPT: Product dashboard in a modern office\\nBeat 2: VISUAL: A close-up of a server rack"
    assert extract_visual_signatures(prompt) == [
        normalize_visual_signature("Product dashboard in a modern office"),
        normalize_visual_signature("A close-up of a server rack"),
    ]
