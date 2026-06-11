"""HTML/CSS slide compositor rendered with Playwright (Chromium).

The AI model renders only photographic backgrounds; this module owns every glyph
via Jinja2 HTML templates screenshotted by Chromium, so typography and branding
are pixel-perfect and identical every day. It composites, per slide:

  full-bleed photo -> left legibility scrim -> logo/wordmark -> gold-highlighted
  headline -> (glass card body | contact block) -> Swipe pill + counter,

with dedicated templates for the hook (slide 1), insight (middle) and cta (last).

HTML generation (:meth:`Compositor.build_slide_html`) is pure and unit-tested;
the Chromium screenshot step is behind the :class:`HtmlScreenshotter` protocol so
tests inject a stub and never launch a browser. Output is always the exact
configured ``slide_size`` (default 1080x1350).
"""

from __future__ import annotations

import base64
import html as html_lib
import io
import logging
import re
from pathlib import Path
from typing import Any, Protocol

from jinja2 import Environment, FileSystemLoader, select_autoescape
from PIL import Image

from gelio.schemas import Slide, SlideRole

logger = logging.getLogger("gelio.compositor")

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
# Variable-font filenames written by `setup-assets`.
FONT_FILES = {"Montserrat": "Montserrat-Variable.ttf", "Inter": "Inter-Variable.ttf"}


class CompositorError(RuntimeError):
    """Raised when required template/render assets are missing or rendering fails."""


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #
def highlight_headline(headline: str, highlights: list[str]) -> str:
    """HTML-escape ``headline`` and wrap each highlight word in a gold span.

    Case-insensitive, first occurrence per word, original casing preserved.
    """
    safe = html_lib.escape(headline)
    for word in highlights or []:
        word = (word or "").strip()
        if not word:
            continue
        pattern = re.compile(re.escape(html_lib.escape(word)), re.IGNORECASE)
        safe = pattern.sub(
            lambda m: f'<span class="gold">{m.group(0)}</span>', safe, count=1
        )
    return safe


def _data_uri(data: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def build_font_faces(fonts_dir: Path) -> str:
    """Return @font-face CSS embedding the local variable TTFs as base64.

    Empty string when fonts are absent (Chromium then falls back to a system
    sans-serif — fine for tests/degraded environments).
    """
    faces: list[str] = []
    for family, filename in FONT_FILES.items():
        path = fonts_dir / filename
        if path.exists():
            uri = _data_uri(path.read_bytes(), "font/ttf")
            faces.append(
                f"@font-face{{font-family:'{family}';font-style:normal;"
                f"font-weight:100 900;src:url({uri}) format('truetype');}}"
            )
    return "\n".join(faces)


# --------------------------------------------------------------------------- #
# Screenshotter
# --------------------------------------------------------------------------- #
class HtmlScreenshotter(Protocol):
    """Renders HTML to PNG bytes at the given CSS pixel size."""

    def screenshot(self, html: str, width: int, height: int) -> bytes: ...


class PlaywrightScreenshotter:
    """Chromium-backed screenshotter; lazily launches and reuses one browser."""

    def __init__(self, device_scale_factor: int = 2) -> None:
        self._dsf = device_scale_factor
        self._pw = None
        self._browser = None

    def _ensure(self) -> None:
        if self._browser is None:
            try:
                from playwright.sync_api import sync_playwright
            except ImportError as exc:  # pragma: no cover - import guard
                raise CompositorError(
                    "playwright is not installed. Run `python run.py setup-assets`."
                ) from exc
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(args=["--no-sandbox"])

    def screenshot(self, html: str, width: int, height: int) -> bytes:
        self._ensure()
        page = self._browser.new_page(  # type: ignore[union-attr]
            viewport={"width": width, "height": height},
            device_scale_factor=self._dsf,
        )
        try:
            page.set_content(html, wait_until="load")
            page.wait_for_timeout(60)  # settle layout/fonts (all assets are inline)
            return page.screenshot(
                clip={"x": 0, "y": 0, "width": width, "height": height}
            )
        finally:
            page.close()

    def close(self) -> None:
        if self._browser is not None:
            self._browser.close()
            self._browser = None
        if self._pw is not None:
            self._pw.stop()
            self._pw = None


# --------------------------------------------------------------------------- #
# Compositor
# --------------------------------------------------------------------------- #
class SlideMeta:
    """What the compositor rendered — used by tests and for logging."""

    def __init__(self, role: str, size: tuple[int, int], texts: list[str]) -> None:
        self.role = role
        self.size = size
        self.texts = texts


_ROLE_TEMPLATE = {
    SlideRole.HOOK: "hook.html.j2",
    SlideRole.INSIGHT: "insight.html.j2",
    SlideRole.CTA: "cta.html.j2",
}


class Compositor:
    """Renders final slides from backgrounds + slide content via HTML templates."""

    def __init__(
        self,
        brand: dict[str, Any],
        screenshotter: HtmlScreenshotter,
        templates_dir: Path = TEMPLATES_DIR,
        fonts_dir: Path | None = None,
        logo_path: Path | None = None,
    ) -> None:
        if not templates_dir.exists():
            raise CompositorError(f"templates dir not found: {templates_dir}")
        self._brand = brand
        self._screenshotter = screenshotter
        visual = brand.get("visual", {})
        self._w, self._h = (int(x) for x in visual.get("slide_size", [1080, 1350]))
        self._env = Environment(
            loader=FileSystemLoader(str(templates_dir)),
            autoescape=select_autoescape(["html", "j2", "html.j2"], default=True),
        )
        self._colors = {
            "navy": visual.get("navy", "#0A1F3D"),
            "navy_panel": visual.get("navy_panel", "#0A1A33"),
            "blue": visual.get("blue", "#0B3D91"),
            "gold": visual.get("gold", "#E8B33D"),
            "text": visual.get("text", "#FFFFFF"),
            "muted": visual.get("muted", "#C9D4E5"),
        }
        self._academy = brand.get("academy_short") or brand.get("name", "")
        self._contact = brand.get("contact", {})
        self._cta_text = brand.get("cta_text", "")
        self._font_faces = build_font_faces(fonts_dir) if fonts_dir else ""
        if fonts_dir and not self._font_faces:
            logger.warning("brand fonts missing in %s; using system fallback", fonts_dir)
        self._logo_uri = self._load_logo(logo_path)

    def _load_logo(self, logo_path: Path | None) -> str | None:
        if logo_path and Path(logo_path).exists():
            suffix = Path(logo_path).suffix.lower()
            mime = "image/svg+xml" if suffix == ".svg" else f"image/{suffix.lstrip('.') or 'png'}"
            return _data_uri(Path(logo_path).read_bytes(), mime)
        return None

    # -- HTML (pure, testable) ----------------------------------------------
    def build_slide_html(
        self, slide: Slide, total: int, *, bg_data_uri: str = ""
    ) -> str:
        template = self._env.get_template(_ROLE_TEMPLATE[slide.role])
        return template.render(
            role=slide.role.value,
            w=self._w,
            h=self._h,
            c=self._colors,
            font_faces=self._font_faces,
            bg_data_uri=bg_data_uri,
            logo_data_uri=self._logo_uri,
            wordmark=self._academy.upper(),
            academy=self._academy,
            index=slide.index,
            total=total,
            eyebrow=slide.eyebrow,
            headline_html=highlight_headline(slide.headline, slide.highlight),
            body=slide.body,
            cta=self._cta_text,
            contact=self._contact,
        )

    # -- render -------------------------------------------------------------
    def compose(
        self, slide: Slide, total: int, background_png_bytes: bytes
    ) -> tuple[Image.Image, SlideMeta]:
        bg_uri = _data_uri(background_png_bytes, "image/png")
        html = self.build_slide_html(slide, total, bg_data_uri=bg_uri)
        png = self._screenshotter.screenshot(html, self._w, self._h)
        img = Image.open(io.BytesIO(png)).convert("RGB")
        if img.size != (self._w, self._h):  # downscale 2x screenshots / normalize stubs
            img = img.resize((self._w, self._h), Image.LANCZOS)

        texts = [slide.headline, slide.body]
        if slide.eyebrow:
            texts.append(slide.eyebrow)
        if slide.role == SlideRole.CTA:
            texts.append(self._cta_text)
            texts.extend(
                str(self._contact.get(k, "")) for k in ("email", "phone", "address")
            )
        return img, SlideMeta(slide.role.value, (self._w, self._h), texts)

    def close(self) -> None:
        close = getattr(self._screenshotter, "close", None)
        if callable(close):
            close()
