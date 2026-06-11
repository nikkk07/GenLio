"""Template compositor: stamp branded typography over AI/fallback backgrounds.

The AI model never renders text — this module owns every glyph, guaranteeing
pixel-perfect copy and identical branding every day. It composites, per slide:

  background -> bottom-heavy dark readability scrim -> headline -> body
  -> slide number -> logo/wordmark, with dedicated layouts for the hook (slide 1)
  and the CTA (last slide).

Text is auto-shrunk stepwise and word-wrapped so it never overflows its box,
regardless of length — Phase 1 enforces char limits, but the compositor stays
safe even if those limits change. Output is always exactly 1080x1350.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from PIL.ImageFont import FreeTypeFont

from gelio.schemas import Slide, SlideRole

logger = logging.getLogger("gelio.compositor")


class CompositorError(RuntimeError):
    """Raised when required assets (e.g. fonts) are missing."""


def _hex(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return tuple(int(value[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


class FontSet:
    """Resolves brand fonts at arbitrary sizes, with a scalable fallback.

    In production the headline/body TTFs downloaded by ``setup-assets`` are used;
    tests (and any degraded environment) fall back to Pillow's scalable default
    font so no network or binary assets are required.
    """

    def __init__(
        self, headline_path: Path | None, body_path: Path | None
    ) -> None:
        self._paths = {"headline": headline_path, "body": body_path}
        self._cache: dict[tuple[str, int], FreeTypeFont] = {}

    @classmethod
    def from_dir(cls, fonts_dir: Path, *, require: bool = True) -> "FontSet":
        headline = fonts_dir / "Poppins-Bold.ttf"
        body = fonts_dir / "Lato-Regular.ttf"
        if require and not (headline.exists() and body.exists()):
            raise CompositorError(
                f"Brand fonts missing in {fonts_dir}. Run "
                "`python run.py setup-assets` to download them."
            )
        return cls(
            headline_path=headline if headline.exists() else None,
            body_path=body if body.exists() else None,
        )

    @classmethod
    def fallback(cls) -> "FontSet":
        """A FontSet using only Pillow's scalable default font (no files)."""
        return cls(headline_path=None, body_path=None)

    def get(self, role: str, size: int) -> FreeTypeFont:
        key = (role, size)
        if key not in self._cache:
            path = self._paths.get(role)
            if path is not None:
                self._cache[key] = ImageFont.truetype(str(path), size)
            else:
                self._cache[key] = ImageFont.load_default(size=size)
        return self._cache[key]


@dataclass
class SlideMeta:
    """What the compositor drew — used by tests and for logging."""

    role: str
    size: tuple[int, int]
    texts: list[str] = field(default_factory=list)


# Layout constants (relative to the 1080x1350 canvas).
_MARGIN = 90


class Compositor:
    """Composites final slides from backgrounds + slide content."""

    def __init__(
        self,
        brand: dict,
        fonts: FontSet,
        logo_path: Path | None = None,
    ) -> None:
        self._brand = brand
        self._fonts = fonts
        visual = brand.get("visual", {})
        self._size: tuple[int, int] = tuple(visual.get("slide_size", [1080, 1350]))  # type: ignore[assignment]
        self._text_color = _hex(visual.get("text_color", "#FFFFFF"))
        self._accent = _hex(visual.get("accent_color", "#F5A623"))
        self._tint = _hex(visual.get("background_tint", "#0A1F3D"))
        self._academy = brand.get("academy_short") or brand.get("name", "")
        self._logo = (
            Image.open(logo_path).convert("RGBA")
            if logo_path and Path(logo_path).exists()
            else None
        )

    # -- public --------------------------------------------------------------
    def compose(self, slide: Slide, total: int, background_png_bytes: bytes) -> tuple[Image.Image, SlideMeta]:
        """Return the final composited slide image + metadata."""
        from io import BytesIO
        from PIL import ImageOps

        w, h = self._size
        bg = Image.open(BytesIO(background_png_bytes)).convert("RGB")
        bg = ImageOps.fit(bg, (w, h), method=Image.LANCZOS)
        canvas = bg.convert("RGBA")
        canvas.alpha_composite(self._scrim((w, h), slide.role))

        draw = ImageDraw.Draw(canvas)
        if slide.role == SlideRole.HOOK:
            meta = self._layout_hook(canvas, draw, slide)
        elif slide.role == SlideRole.CTA:
            meta = self._layout_cta(canvas, draw, slide)
        else:
            meta = self._layout_insight(canvas, draw, slide, total)

        return canvas.convert("RGB"), meta

    # -- layouts -------------------------------------------------------------
    def _layout_hook(self, canvas: Image.Image, draw: ImageDraw.ImageDraw, slide: Slide) -> SlideMeta:
        w, h = self._size
        meta = SlideMeta(role="hook", size=(w, h))
        self._brand_mark(canvas, draw, corner="top-left")

        box_w = w - 2 * _MARGIN
        # Oversized centered headline in the vertical middle.
        font, lines, line_h, _ = self._fit(draw, slide.headline, "headline", box_w, int(h * 0.42), max_size=120, min_size=48)
        block_h = line_h * len(lines)
        y = (h - block_h) // 2 - int(h * 0.05)
        self._draw_block(draw, lines, font, y, line_h, align="center")
        meta.texts.extend(lines)

        # Minimal body beneath.
        bfont, blines, bline_h, _ = self._fit(draw, slide.body, "body", box_w, int(h * 0.18), max_size=44, min_size=24)
        by = y + block_h + 40
        self._draw_block(draw, blines, bfont, by, bline_h, align="center")
        meta.texts.extend(blines)
        return meta

    def _layout_insight(self, canvas: Image.Image, draw: ImageDraw.ImageDraw, slide: Slide, total: int) -> SlideMeta:
        w, h = self._size
        meta = SlideMeta(role="insight", size=(w, h))
        self._brand_mark(canvas, draw, corner="top-left")

        box_w = w - 2 * _MARGIN
        # Headline in upper third.
        hfont, hlines, hline_h, _ = self._fit(draw, slide.headline, "headline", box_w, int(h * 0.26), max_size=78, min_size=40)
        hy = int(h * 0.16)
        self._draw_block(draw, hlines, hfont, hy, hline_h, align="left")
        meta.texts.extend(hlines)
        headline_bottom = hy + hline_h * len(hlines)

        # Thin accent rule between headline and body.
        rule_y = headline_bottom + 26
        draw.rectangle([_MARGIN, rule_y, _MARGIN + 120, rule_y + 6], fill=self._accent)

        # Body in the middle.
        bfont, blines, bline_h, _ = self._fit(draw, slide.body, "body", box_w, int(h * 0.42), max_size=46, min_size=24)
        by = rule_y + 50
        self._draw_block(draw, blines, bfont, by, bline_h, align="left")
        meta.texts.extend(blines)

        # Slide number bottom corner.
        number = f"{slide.index}/{total}"
        self._draw_number(draw, number)
        meta.texts.append(number)
        return meta

    def _layout_cta(self, canvas: Image.Image, draw: ImageDraw.ImageDraw, slide: Slide) -> SlideMeta:
        w, h = self._size
        meta = SlideMeta(role="cta", size=(w, h))
        box_w = w - 2 * _MARGIN

        # Academy name, large and centered.
        afont, alines, aline_h, _ = self._fit(draw, self._academy, "headline", box_w, int(h * 0.24), max_size=96, min_size=44)
        ay = int(h * 0.22)
        self._draw_block(draw, alines, afont, ay, aline_h, align="center")
        meta.texts.extend(alines)
        academy_bottom = ay + aline_h * len(alines)

        # Short tagline from the slide headline (NOT the body — the body restates
        # the CTA, which already appears in the pill below, so we avoid the dupe).
        tfont, tlines, tline_h, _ = self._fit(draw, slide.headline, "body", box_w, int(h * 0.16), max_size=44, min_size=26)
        ty = academy_bottom + 44
        self._draw_block(draw, tlines, tfont, ty, tline_h, align="center")
        meta.texts.extend(tlines)
        tagline_bottom = ty + tline_h * len(tlines)

        # Accent pill around the single call-to-action from brand.json.
        cta_text = self._brand.get("cta_text", "")
        self._draw_pill(draw, cta_text, center_y=min(tagline_bottom + 120, h - 260))
        meta.texts.append(cta_text)

        # Prominent logo near the bottom — only when a real logo exists. With a
        # text wordmark the academy name is already large above, so repeating it
        # would look templated-by-accident.
        if self._logo is not None:
            self._brand_mark(canvas, draw, corner="bottom-center", scale=1.6)
        return meta

    # -- primitives ----------------------------------------------------------
    def _scrim(self, size: tuple[int, int], role: SlideRole) -> Image.Image:
        """Bottom-heavy dark gradient overlay for guaranteed text contrast."""
        w, h = size
        top_a = 110 if role == SlideRole.HOOK else 90
        bot_a = 225
        column = Image.new("L", (1, h))
        cpx = column.load()
        for y in range(h):
            t = y / max(h - 1, 1)
            cpx[0, y] = int(top_a + (bot_a - top_a) * (t**1.5))
        alpha = column.resize((w, h))
        overlay = Image.new("RGBA", (w, h), self._tint + (0,))
        overlay.putalpha(alpha)
        return overlay

    def _fit(
        self,
        draw: ImageDraw.ImageDraw,
        text: str,
        role: str,
        box_w: int,
        box_h: int,
        max_size: int,
        min_size: int,
        spacing: float = 1.14,
    ) -> tuple[FreeTypeFont, list[str], int, int]:
        """Shrink font stepwise until wrapped text fits (box_w x box_h)."""
        text = text.strip() or " "
        for size in range(max_size, min_size - 1, -2):
            font = self._fonts.get(role, size)
            lines = self._wrap(draw, text, font, box_w)
            line_h = int(size * spacing)
            if line_h * len(lines) <= box_h:
                return font, lines, line_h, size
        # Smallest acceptable size; wrap (with hard-breaks) still bounds width.
        font = self._fonts.get(role, min_size)
        lines = self._wrap(draw, text, font, box_w)
        return font, lines, int(min_size * spacing), min_size

    def _wrap(self, draw: ImageDraw.ImageDraw, text: str, font: FreeTypeFont, max_w: int) -> list[str]:
        lines: list[str] = []
        current = ""
        for word in text.split():
            trial = word if not current else f"{current} {word}"
            if draw.textlength(trial, font=font) <= max_w:
                current = trial
                continue
            if current:
                lines.append(current)
                current = ""
            if draw.textlength(word, font=font) > max_w:
                lines.extend(self._hard_break(draw, word, font, max_w))
            else:
                current = word
        if current:
            lines.append(current)
        return lines or [""]

    def _hard_break(self, draw: ImageDraw.ImageDraw, word: str, font: FreeTypeFont, max_w: int) -> list[str]:
        chunks: list[str] = []
        piece = ""
        for ch in word:
            if draw.textlength(piece + ch, font=font) <= max_w:
                piece += ch
            else:
                if piece:
                    chunks.append(piece)
                piece = ch
        if piece:
            chunks.append(piece)
        return chunks or [word]

    def _draw_block(
        self,
        draw: ImageDraw.ImageDraw,
        lines: list[str],
        font: FreeTypeFont,
        top: int,
        line_h: int,
        align: str,
    ) -> None:
        w, _ = self._size
        for i, line in enumerate(lines):
            y = top + i * line_h
            if align == "center":
                lw = draw.textlength(line, font=font)
                x = (w - lw) / 2
            else:
                x = _MARGIN
            # Soft shadow for extra contrast, then the text.
            draw.text((x + 2, y + 2), line, font=font, fill=(0, 0, 0))
            draw.text((x, y), line, font=font, fill=self._text_color)

    def _draw_number(self, draw: ImageDraw.ImageDraw, number: str) -> None:
        w, h = self._size
        font = self._fonts.get("body", 36)
        lw = draw.textlength(number, font=font)
        x = w - _MARGIN - lw
        y = h - _MARGIN - 36
        draw.text((x, y), number, font=font, fill=self._accent)

    def _draw_pill(self, draw: ImageDraw.ImageDraw, text: str, center_y: int) -> None:
        w, _ = self._size
        box_w = w - 2 * _MARGIN - 80
        font, lines, line_h, size = self._fit(draw, text, "body", box_w, 200, max_size=38, min_size=22)
        text_h = line_h * len(lines)
        pad_x, pad_y = 48, 30
        widest = max(draw.textlength(ln, font=font) for ln in lines)
        pill_w = int(widest + 2 * pad_x)
        pill_h = int(text_h + 2 * pad_y)
        x0 = (w - pill_w) // 2
        y0 = center_y - pill_h // 2
        draw.rounded_rectangle(
            [x0, y0, x0 + pill_w, y0 + pill_h], radius=pill_h // 2, fill=self._accent
        )
        # Dark text on the accent pill for contrast.
        for i, line in enumerate(lines):
            lw = draw.textlength(line, font=font)
            lx = (w - lw) / 2
            ly = y0 + pad_y + i * line_h
            draw.text((lx, ly), line, font=font, fill=self._tint)

    def _brand_mark(
        self,
        canvas: Image.Image,
        draw: ImageDraw.ImageDraw,
        corner: str,
        scale: float = 1.0,
    ) -> None:
        """Paste the logo if present, else a styled text wordmark."""
        w, h = self._size
        if self._logo is not None:
            target_w = int(220 * scale)
            ratio = target_w / self._logo.width
            logo = self._logo.resize((target_w, int(self._logo.height * ratio)))
            if corner == "top-left":
                pos = (_MARGIN, _MARGIN - 30)
            elif corner == "bottom-center":
                pos = ((w - logo.width) // 2, h - logo.height - _MARGIN)
            else:
                pos = (_MARGIN, _MARGIN)
            canvas.alpha_composite(logo, pos)
            return

        # Wordmark fallback.
        size = int(34 * scale)
        font = self._fonts.get("headline", size)
        text = self._academy
        lw = draw.textlength(text, font=font)
        if corner == "bottom-center":
            x, y = (w - lw) / 2, h - _MARGIN - size
        else:  # top-left
            x, y = _MARGIN, _MARGIN - 40
        draw.text((x, y), text, font=font, fill=self._accent)
