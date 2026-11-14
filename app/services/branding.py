"""Branded end-card rendering (gradient, logo/wordmark, tagline).

Mirrors the reference video's closing shot: a diagonal brand-gradient card
with the platform wordmark, an English tagline, and the site URL. Rendered
with Pillow as a static PNG; the assembly stage appends it as the final
scene of the deliverable.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# Reference-video palette: deep purple base with a pink/violet diagonal sweep.
_BG_TOP = (34, 16, 64)
_BG_BOTTOM = (78, 24, 128)
_SWEEP_TOP = (233, 64, 143)
_SWEEP_BOTTOM = (120, 60, 220)
_TEXT_COLOR = (255, 255, 255)
_SUB_COLOR = (235, 230, 245)


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
    candidates += [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/farsiweb/nazli.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/tahoma.ttf",
        "C:/Windows/Fonts/segoeui.ttf",
    ]
    return _load_font(size, [c for c in candidates if c])


def render_end_card(
    *,
    output_path: Path,
    width: int,
    height: int,
    title: str,
    tagline: str = "",
    site_url: str = "",
    logo_path: str | Path | None = None,
) -> Path:
    """Render the branded closing card and return its path."""
    card = Image.new("RGB", (width, height), _BG_TOP)
    draw = ImageDraw.Draw(card)

    # Vertical gradient base.
    for y in range(height):
        t = y / max(1, height - 1)
        color = tuple(int(a + (b - a) * t) for a, b in zip(_BG_TOP, _BG_BOTTOM))
        draw.line([(0, y), (width, y)], fill=color)

    # Diagonal sweep bands (top-left to bottom-right accent), like the reference.
    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    ov_draw = ImageDraw.Draw(overlay)
    band = int(width * 0.55)
    for i in range(band):
        t = i / max(1, band - 1)
        color = tuple(int(a + (b - a) * t) for a, b in zip(_SWEEP_TOP, _SWEEP_BOTTOM))
        x0 = int(-width * 0.35 + i * (width * 1.6 / band))
        alpha = int(210 * (1 - abs(t - 0.5) * 1.2))
        if alpha <= 0:
            continue
        ov_draw.line([(x0, height), (x0 + int(width * 0.5), 0)], fill=(*color, alpha), width=int(height * 0.02))
    card = Image.alpha_composite(card.convert("RGBA"), overlay).convert("RGB")
    draw = ImageDraw.Draw(card)

    # Optional logo centered above the wordmark.
    cursor_y = int(height * 0.30)
    if logo_path and Path(logo_path).is_file():
        logo = Image.open(logo_path).convert("RGBA")
        target_w = int(width * 0.30)
        ratio = target_w / max(1, logo.width)
        logo = logo.resize((target_w, max(1, int(logo.height * ratio))))
        card.paste(logo, ((width - logo.width) // 2, cursor_y), logo)
        cursor_y += logo.height + int(height * 0.04)

    title_font = _find_font("DejaVuSans-Bold", int(width * 0.115))
    sub_font = _find_font("DejaVuSans", int(width * 0.045))
    url_font = _find_font("DejaVuSans", int(width * 0.038))

    def _center(text: str, font, fill, y: int) -> int:
        box = draw.textbbox((0, 0), text, font=font)
        text_w = box[2] - box[0]
        text_h = box[3] - box[1]
        draw.text(((width - text_w) // 2, y), text, font=font, fill=fill)
        return y + text_h + int(height * 0.025)

    cursor_y = _center(title.upper(), title_font, _TEXT_COLOR, cursor_y)
    if tagline:
        for line in tagline.split("\n"):
            cursor_y = _center(line, sub_font, _SUB_COLOR, cursor_y)
    if site_url:
        _center(site_url, url_font, _SUB_COLOR, int(height * 0.88))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    card.save(output_path, format="PNG")
    return output_path
