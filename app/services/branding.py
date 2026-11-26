"""Branded end-card rendering with exact-text, RTL-safe Persian typography.

Mirrors the reference video's closing shot: a diagonal brand-gradient card with
the platform wordmark, an optional product/title line, the authored CTA and the
site URL. Text is never rewritten, translated, upper-cased or re-ordered in the
source data: Persian is reshaped and bidi-reordered only for rasterisation, so
the rendered string is the exact text supplied by the brand/operator.

The card is rendered with Pillow as a static PNG and appended as the final
scene by the assembly stage. A mobile safe area keeps every line away from the
frame edges and shrinks long text deterministically instead of clipping it.
"""

from __future__ import annotations

import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

try:  # Optional at import time; pyproject installs both for production images.
    import arabic_reshaper  # noqa: PLC0415
    from bidi.algorithm import get_display  # noqa: PLC0415
except ImportError:  # pragma: no cover - exercised only in minimal environments
    arabic_reshaper = None  # type: ignore[assignment]
    get_display = None  # type: ignore[assignment]

# Reference-video palette: deep purple base with a pink/violet diagonal sweep.
_BG_TOP = (34, 16, 64)
_BG_BOTTOM = (78, 24, 128)
_SWEEP_TOP = (233, 64, 143)
_SWEEP_BOTTOM = (120, 60, 220)
_TEXT_COLOR = (255, 255, 255)
_SUB_COLOR = (235, 230, 245)
_PERSIAN_RE = re.compile(r"[\u0600-\u06FF]")

_FONT_CANDIDATES: dict[str, tuple[str, ...]] = {
    "bold": (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/farsiweb/nazli.ttf",
        "C:/Windows/Fonts/tahomabd.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
        "C:/Windows/Fonts/seguisb.ttf",
    ),
    "regular": (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/farsiweb/nazli.ttf",
        "C:/Windows/Fonts/tahoma.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/segoeui.ttf",
    ),
}


def _load_font(size: int, candidates: list[str]) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in candidates:
        if Path(path).is_file():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def _find_font(name: str, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    import shutil  # noqa: PLC0415 - cheap and local

    hits = shutil.which(name)
    candidates = [hits] if hits else []
    candidates += list(_FONT_CANDIDATES["bold" if "bold" in name.lower() else "regular"])
    return _load_font(size, [c for c in candidates if c])


def display_text(text: str) -> str:
    """Return the rasterisation form of ``text`` without changing its content.

    Persian is reshaped and then bidi-reordered. Latin-only strings are left
    untouched (including URLs), so brand text is never accidentally upper-cased
    or reversed by the typography helper.
    """
    value = str(text or "")
    if not _PERSIAN_RE.search(value):
        return value
    if arabic_reshaper is None or get_display is None:
        return value
    return get_display(arabic_reshaper.reshape(value))


def _text_size(draw: ImageDraw.ImageDraw, text: str, font) -> tuple[int, int]:
    box = draw.textbbox((0, 0), display_text(text), font=font)
    return box[2] - box[0], box[3] - box[1]


def wrap_text(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> list[str]:
    """Word-wrap exact text to the safe width; no characters are dropped."""
    lines: list[str] = []
    for paragraph in str(text or "").split("\n"):
        words = paragraph.split(" ")
        current = ""
        for word in words:
            candidate = f"{current} {word}".strip()
            width, _ = _text_size(draw, candidate, font)
            if current and width > max_width:
                lines.append(current)
                current = word
            else:
                current = candidate
        lines.append(current)
    return lines


def fit_text_block(
    draw: ImageDraw.ImageDraw,
    text: str,
    *,
    font_name: str,
    base_size: int,
    max_width: int,
    max_height: int,
    line_gap: int,
) -> tuple[list[str], object]:
    """Shrink a text block until every exact line fits inside the safe area."""
    minimum = max(8, base_size // 3)
    size = base_size
    while size >= minimum:
        font = _find_font(font_name, size)
        lines = wrap_text(draw, text, font, max_width)
        _, line_height = _text_size(draw, "نگ", font)
        height = len(lines) * max(1, line_height) + max(0, len(lines) - 1) * line_gap
        if height <= max_height and all(
            _text_size(draw, line, font)[0] <= max_width for line in lines
        ):
            return lines, font
        size = max(minimum - 1, int(size * 0.9))
    font = _find_font(font_name, minimum)
    return wrap_text(draw, text, font, max_width), font


def _draw_block(
    draw: ImageDraw.ImageDraw,
    *,
    text: str,
    font_name: str,
    base_size: int,
    y: int,
    max_y: int,
    max_width: int,
    left: int,
    fill,
) -> int:
    if not str(text or "").strip():
        return y
    line_gap = max(2, int(max_width * 0.01))
    lines, font = fit_text_block(
        draw,
        text,
        font_name=font_name,
        base_size=base_size,
        max_width=max_width,
        max_height=max(1, max_y - y),
        line_gap=line_gap,
    )
    _, line_height = _text_size(draw, "نگ", font)
    for line in lines:
        width, _ = _text_size(draw, line, font)
        draw.text((left + (max_width - width) // 2, y), display_text(line),
                  font=font, fill=fill)
        y += max(1, line_height) + line_gap
    return y


def render_end_card(
    *,
    output_path: Path,
    width: int,
    height: int,
    title: str,
    tagline: str = "",
    site_url: str = "",
    logo_path: str | Path | None = None,
    product: str = "",
    cta: str = "",
) -> Path:
    """Render the branded closing card and return its path.

    ``title`` is the brand wordmark. ``product``, ``cta`` and ``site_url`` are
    authored strings and are rendered verbatim (with RTL shaping for Persian).
    All text is confined to a mobile safe area and shrunk rather than clipped.
    """
    if width <= 0 or height <= 0:
        raise ValueError("end-card dimensions must be positive")

    card = Image.new("RGB", (width, height), _BG_TOP)
    draw = ImageDraw.Draw(card)

    # Vertical gradient base.
    for y in range(height):
        t = y / max(1, height - 1)
        color = tuple(int(a + (b - a) * t) for a, b in zip(_BG_TOP, _BG_BOTTOM, strict=False))
        draw.line([(0, y), (width, y)], fill=color)

    # Diagonal sweep bands (top-left to bottom-right accent), like the reference.
    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    ov_draw = ImageDraw.Draw(overlay)
    band = int(width * 0.55)
    for i in range(band):
        t = i / max(1, band - 1)
        color = tuple(int(a + (b - a) * t) for a, b in zip(_SWEEP_TOP, _SWEEP_BOTTOM, strict=False))
        x0 = int(-width * 0.35 + i * (width * 1.6 / band))
        alpha = int(210 * (1 - abs(t - 0.5) * 1.2))
        if alpha <= 0:
            continue
        ov_draw.line(
            [(x0, height), (x0 + int(width * 0.5), 0)],
            fill=(*color, alpha),
            width=max(1, int(height * 0.02)),
        )
    card = Image.alpha_composite(card.convert("RGBA"), overlay).convert("RGB")
    draw = ImageDraw.Draw(card)

    safe_left = int(width * 0.08)
    safe_right = int(width * 0.92)
    safe_width = safe_right - safe_left
    y = int(height * 0.25)

    if logo_path and Path(logo_path).is_file():
        logo = Image.open(logo_path).convert("RGBA")
        target_w = min(safe_width, int(width * 0.30))
        ratio = target_w / max(1, logo.width)
        logo = logo.resize((target_w, max(1, int(logo.height * ratio))))
        card.paste(logo, ((width - logo.width) // 2, y), logo)
        draw = ImageDraw.Draw(card)
        y += logo.height + int(height * 0.03)

    y = _draw_block(
        draw, text=title, font_name="bold", base_size=int(width * 0.10),
        y=y, max_y=int(height * 0.55), max_width=safe_width, left=safe_left, fill=_TEXT_COLOR,
    )
    y = _draw_block(
        draw, text=product, font_name="bold", base_size=int(width * 0.06),
        y=y, max_y=int(height * 0.64), max_width=safe_width, left=safe_left, fill=_SUB_COLOR,
    )
    y = _draw_block(
        draw, text=tagline, font_name="regular", base_size=int(width * 0.043),
        y=y, max_y=int(height * 0.75), max_width=safe_width, left=safe_left, fill=_SUB_COLOR,
    )
    y = _draw_block(
        draw, text=cta, font_name="bold", base_size=int(width * 0.052),
        y=y, max_y=int(height * 0.84), max_width=safe_width, left=safe_left, fill=_TEXT_COLOR,
    )
    if site_url:
        _draw_block(
            draw, text=site_url, font_name="regular", base_size=int(width * 0.036),
            y=int(height * 0.88), max_y=int(height * 0.96),
            max_width=safe_width, left=safe_left, fill=_SUB_COLOR,
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    card.save(output_path, format="PNG")
    return output_path


__all__ = ["display_text", "fit_text_block", "render_end_card", "wrap_text"]
