"""Character reference-image registry tests (operator-managed assets)."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.services.character_service import (
    CharacterImageError,
    list_character_images,
    load_character_image_bytes,
    resolve_character_image,
)

_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 1100  # magic + padding; validator checks magic bytes
_JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 1100
_WEBP_BYTES = b"RIFF\x24\x00\x00\x00WEBPVP8 \x00\x00\x00\x00" + b"\x00" * 1100


@pytest.fixture
def characters_root(tmp_path: Path) -> Path:
    root = tmp_path / "Paltform_charactar" / "Vandiyar"
    root.mkdir(parents=True)
    (root / "Emerald Blazer.png").write_bytes(_PNG_BYTES)
    (root / "Floral Hijab.jpg").write_bytes(_JPEG_BYTES)
    (root / "Reference.webp").write_bytes(_WEBP_BYTES)
    (root / "notes.txt").write_bytes(b"not an image")
    return root.parent


def test_list_character_images_only_returns_supported_formats(characters_root):
    images = list_character_images(characters_root)
    keys = {image.key for image in images}
    assert keys == {
        "Vandiyar/Emerald Blazer",
        "Vandiyar/Floral Hijab",
        "Vandiyar/Reference",
    }
    emerald = next(image for image in images if image.key.endswith("Emerald Blazer"))
    assert emerald.content_type == "image/png"
    assert emerald.file_name == "Emerald Blazer.png"


def test_resolve_by_stem_and_by_full_filename(characters_root):
    by_stem = resolve_character_image(characters_root, "Vandiyar/Emerald Blazer")
    by_file = resolve_character_image(characters_root, "Vandiyar/Emerald Blazer.png")
    assert by_stem.path == by_file.path
    assert load_character_image_bytes(by_stem).startswith(b"\x89PNG")


def test_resolve_missing_or_blank_key_raises_actionable_error(characters_root):
    with pytest.raises(CharacterImageError) as error:
        resolve_character_image(characters_root, "   ")
    assert error.value.code == "character_image_missing"
    with pytest.raises(CharacterImageError) as error:
        resolve_character_image(characters_root, "Vandiyar/Does Not Exist")
    assert error.value.code == "character_image_not_found"
    assert "Vandiyar/Emerald Blazer" in error.value.message


def test_resolve_rejects_oversized_or_corrupt_images(tmp_path):
    root = tmp_path / "chars" / "Vandiyar"
    root.mkdir(parents=True)
    (root / "Huge.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * (10 * 1024 * 1024 + 1))
    (root / "Corrupt.png").write_bytes(b"\x00" * 2048)
    with pytest.raises(CharacterImageError) as error:
        resolve_character_image(root.parent, "Vandiyar/Huge")
    assert error.value.code == "character_image_too_large"
    with pytest.raises(CharacterImageError) as error:
        resolve_character_image(root.parent, "Vandiyar/Corrupt")
    assert error.value.code == "character_image_invalid"


def test_resolve_without_root_configuration_raises():
    with pytest.raises(CharacterImageError) as error:
        resolve_character_image(None, "Vandiyar/Anything")
    assert error.value.code == "character_root_unconfigured"


def test_resolve_when_directory_missing_returns_not_found(tmp_path):
    with pytest.raises(CharacterImageError) as error:
        resolve_character_image(tmp_path / "missing", "Vandiyar/Anything")
    assert error.value.code == "character_image_not_found"
