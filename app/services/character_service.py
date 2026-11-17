"""Character reference-image registry for image-to-video providers.

Character reference images live on disk under a configurable root, organised
per platform folder::

    <root>/<Platform>/<character name>.png

The directory is read-only operator-managed content (mounted into the API and
worker containers the same way as the screenshots root). The service resolves
a character key ``"<platform>/<file stem or filename>"`` to one image file and
validates it against the image-to-video provider's constraints (JPEG, PNG or
WEBP, size bounds) BEFORE any paid generation is submitted.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# Image constraints mirror the documented image-to-video input limits:
# JPEG, PNG and WEBP with a 10MB per-image maximum.
SUPPORTED_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MIN_IMAGE_BYTES = 1024

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC = b"\xff\xd8\xff"


class CharacterImageError(Exception):
    """A character reference image is missing, ambiguous or invalid."""

    def __init__(self, message: str, *, code: str = "character_image_invalid") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


@dataclass(frozen=True)
class CharacterImage:
    """One validated operator-managed character reference image."""

    key: str  # "<platform>/<file stem>"
    platform: str
    name: str  # file stem
    path: Path
    size_bytes: int
    content_type: str

    @property
    def file_name(self) -> str:
        return f"{self.name}{self.path.suffix.lower()}"


def list_character_images(root: str | Path | None) -> list[CharacterImage]:
    """List every supported character image under *root*, sorted for stable UIs."""
    if not root:
        return []
    base = Path(root)
    if not base.is_dir():
        return []
    found: list[CharacterImage] = []
    for platform_dir in sorted(entry for entry in base.iterdir() if entry.is_dir()):
        for image_path in sorted(platform_dir.iterdir()):
            if not image_path.is_file():
                continue
            if image_path.suffix.lower() not in SUPPORTED_IMAGE_SUFFIXES:
                continue
            key = f"{platform_dir.name}/{image_path.stem}"
            found.append(
                CharacterImage(
                    key=key,
                    platform=platform_dir.name,
                    name=image_path.stem,
                    path=image_path,
                    size_bytes=image_path.stat().st_size,
                    content_type=_content_type_for(image_path.suffix.lower()),
                )
            )
    return found


def resolve_character_image(root: str | Path | None, key: str) -> CharacterImage:
    """Resolve one character key to a validated image; raise on any ambiguity.

    The key is ``"<platform>/<file stem or filename>"``. Path traversal is
    impossible by construction: both segments are matched against the scanned
    registry instead of being joined into a filesystem path.
    """
    if not root:
        raise CharacterImageError(
            "The character-image root is not configured; set PLATFORM_CHARACTERS_ROOT.",
            code="character_root_unconfigured",
        )
    normalized = (key or "").strip()
    if not normalized:
        raise CharacterImageError(
            "A character reference image is required for image-to-video generation. "
            "Select one via 'character_key' (for example 'Vandiyar/<character name>') "
            "or provide 'character_image_url'.",
            code="character_image_missing",
        )
    images = list_character_images(root)
    stem_keys: dict[str, list[CharacterImage]] = {}
    file_keys: dict[str, CharacterImage] = {}
    for image in images:
        stem_keys.setdefault(f"{image.platform}/{image.name}".lower(), []).append(image)
        file_keys[f"{image.platform}/{image.file_name}".lower()] = image
    lowered = normalized.lower()
    direct = file_keys.get(lowered)
    if direct is None:
        stem_matches = stem_keys.get(lowered, [])
        if len(stem_matches) == 1:
            direct = stem_matches[0]
        elif len(stem_matches) > 1:
            raise CharacterImageError(
                f"The character key '{normalized}' matches multiple files "
                f"({', '.join(sorted(image.file_name for image in stem_matches))}); "
                "include the file extension to pick one.",
                code="character_image_ambiguous",
            )
    if direct is not None:
        return validate_character_image(direct)
    available = ", ".join(sorted({image.key for image in images})) or "(none found)"
    raise CharacterImageError(
        f"No character image matches '{normalized}'. Available keys: {available}.",
        code="character_image_not_found",
    )


def validate_character_image(image: CharacterImage) -> CharacterImage:
    """Validate one image file's size and magic bytes before paid generation."""
    path = image.path
    if not path.is_file():
        raise CharacterImageError(
            f"The character image for '{image.key}' disappeared from storage.",
            code="character_image_missing",
        )
    size = path.stat().st_size
    if size < MIN_IMAGE_BYTES:
        raise CharacterImageError(
            f"The character image '{image.key}' is too small ({size} bytes) to be a usable reference.",
            code="character_image_invalid",
        )
    if size > MAX_IMAGE_BYTES:
        raise CharacterImageError(
            f"The character image '{image.key}' is {size} bytes; the provider accepts at most "
            f"{MAX_IMAGE_BYTES} bytes per image.",
            code="character_image_too_large",
        )
    with path.open("rb") as handle:
        header = handle.read(16)
    if path.suffix.lower() in {".jpg", ".jpeg"}:
        if not header.startswith(_JPEG_MAGIC):
            raise CharacterImageError(
                f"The character image '{image.key}' has a JPEG extension but not JPEG content.",
                code="character_image_invalid",
            )
    elif path.suffix.lower() == ".png":
        if not header.startswith(_PNG_MAGIC):
            raise CharacterImageError(
                f"The character image '{image.key}' has a PNG extension but not PNG content.",
                code="character_image_invalid",
            )
    elif path.suffix.lower() == ".webp":
        if not (header.startswith(b"RIFF") and header[8:12] == b"WEBP"):
            raise CharacterImageError(
                f"The character image '{image.key}' has a WEBP extension but not WEBP content.",
                code="character_image_invalid",
            )
    return image


def load_character_image_bytes(image: CharacterImage) -> bytes:
    """Read validated image bytes for upload to the provider's file storage."""
    validate_character_image(image)
    return image.path.read_bytes()


def _content_type_for(suffix: str) -> str:
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
    }[suffix]


__all__ = [
    "CharacterImage",
    "CharacterImageError",
    "MAX_IMAGE_BYTES",
    "SUPPORTED_IMAGE_SUFFIXES",
    "list_character_images",
    "load_character_image_bytes",
    "resolve_character_image",
    "validate_character_image",
]
