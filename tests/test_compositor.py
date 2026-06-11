"""compositor tests: HTML generation + stub screenshotter (no browser/network)."""

from __future__ import annotations

import io

import pytest
from PIL import Image

from gelio.compositor import Compositor, highlight_headline
from gelio.schemas import BODY_MAX, HEADLINE_MAX, Slide, SlideRole
from tests.conftest import BRAND

W, H = 1080, 1350


class StubShot:
    """Records the HTML it's asked to render; returns a tiny valid PNG."""

    def __init__(self):
        self.last_html = None
        self.last_size = None

    def screenshot(self, html, width, height) -> bytes:
        self.last_html = html
        self.last_size = (width, height)
        buf = io.BytesIO()
        Image.new("RGB", (16, 20), (0, 0, 0)).save(buf, "PNG")
        return buf.getvalue()


def _compositor(logo_path=None) -> tuple[Compositor, StubShot]:
    shot = StubShot()
    return Compositor(BRAND, screenshotter=shot, fonts_dir=None, logo_path=logo_path), shot


def _slide(index, role, headline="Headline here", body="Body text.", highlight=None, eyebrow="LABEL"):
    return Slide(
        index=index,
        role=role,
        headline=headline,
        body=body,
        visual_direction="a scene",
        highlight=highlight or [],
        eyebrow=eyebrow,
    )


def test_highlight_wraps_correct_words_in_gold():
    html = highlight_headline("Only rich people fly", ["rich"])
    assert '<span class="gold">rich</span>' in html
    assert "Only " in html and " people fly" in html


def test_highlight_escapes_html():
    html = highlight_headline("A <b> & C", [])
    assert "&lt;b&gt;" in html and "&amp;" in html


def test_all_roles_render_to_exact_slide_size():
    comp, _ = _compositor()
    for role, idx in [(SlideRole.HOOK, 1), (SlideRole.INSIGHT, 4), (SlideRole.CTA, 9)]:
        img, meta = comp.compose(_slide(idx, role), 9, _png())
        assert img.size == (W, H)
        assert meta.size == (W, H)
        assert img.mode == "RGB"


def test_long_headline_wraps_without_overflow():
    # The compositor must not raise on max-length text; CSS clamp/wrap handle it.
    comp, shot = _compositor()
    headline = "W" * HEADLINE_MAX
    body = "word " * (BODY_MAX // 5)
    img, _ = comp.compose(_slide(1, SlideRole.HOOK, headline[:HEADLINE_MAX], body[:BODY_MAX]), 9, _png())
    assert img.size == (W, H)
    assert "overflow: hidden" in shot.last_html  # content box clips as a safety net


def test_keyword_highlight_in_rendered_html():
    comp, _ = _compositor()
    html = comp.build_slide_html(
        _slide(1, SlideRole.HOOK, "Only rich people fly", highlight=["rich"]), 9
    )
    assert '<span class="gold">rich</span>' in html


def test_hook_has_swipe_cta_does_not():
    comp, _ = _compositor()
    hook = comp.build_slide_html(_slide(1, SlideRole.HOOK), 9)
    cta = comp.build_slide_html(_slide(9, SlideRole.CTA), 9)
    assert 'class="swipe"' in hook
    assert 'class="swipe"' not in cta


def test_cta_contains_contact_details():
    comp, _ = _compositor()
    html = comp.build_slide_html(_slide(9, SlideRole.CTA), 9)
    c = BRAND["contact"]
    assert c["email"] in html
    assert c["phone"] in html
    assert c["address"] in html


def test_logo_missing_falls_back_to_wordmark():
    comp, _ = _compositor(logo_path=None)
    html = comp.build_slide_html(_slide(1, SlideRole.HOOK), 9)
    assert "WE ONE AVIATION" in html
    assert 'class="wordmark"' in html


def test_logo_present_embeds_data_uri(tmp_path):
    logo = tmp_path / "logo.png"
    Image.new("RGBA", (10, 10), (0, 0, 0, 0)).save(logo)
    comp, _ = _compositor(logo_path=logo)
    html = comp.build_slide_html(_slide(1, SlideRole.HOOK), 9)
    assert "data:image/png;base64," in html
    assert 'class="logo"' in html


def test_slide_count_preserved():
    comp, _ = _compositor()
    slides = (
        [_slide(1, SlideRole.HOOK)]
        + [_slide(i, SlideRole.INSIGHT) for i in range(2, 9)]
        + [_slide(9, SlideRole.CTA)]
    )
    imgs = [comp.compose(s, 9, _png())[0] for s in slides]
    assert len(imgs) == 9 and all(im.size == (W, H) for im in imgs)


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (12, 15), (12, 31, 61)).save(buf, "PNG")
    return buf.getvalue()
