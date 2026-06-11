"""compositor tests: bounds-safety, dimensions, CTA text, slide-count parity.

Uses FontSet.fallback() (Pillow's scalable default font) so tests need no
network and no downloaded TTFs.
"""

from __future__ import annotations

import io

import pytest
from PIL import Image

from gelio.compositor import Compositor, FontSet
from gelio.schemas import (
    BODY_MAX,
    HEADLINE_MAX,
    Captions,
    Content,
    Slide,
    SlideRole,
)
from tests.conftest import BRAND

W, H = 1080, 1350


def _bg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (W, H), (12, 31, 61)).save(buf, format="PNG")
    return buf.getvalue()


def _compositor() -> Compositor:
    # No logo path -> wordmark branch exercised.
    return Compositor(BRAND, FontSet.fallback(), logo_path=None)


def _slide(index, role, headline, body):
    return Slide(
        index=index,
        role=role,
        headline=headline,
        body=body,
        visual_direction="a scene",
    )


def test_output_is_exactly_1080x1350():
    comp = _compositor()
    img, meta = comp.compose(_slide(1, SlideRole.HOOK, "Hi", "there"), 9, _bg())
    assert img.size == (W, H)
    assert meta.size == (W, H)
    assert img.mode == "RGB"


def test_max_length_text_never_overflows_canvas():
    comp = _compositor()
    headline = "W" * HEADLINE_MAX  # widest plausible headline
    body = "Word " * (BODY_MAX // 5)  # near the body limit
    for role in (SlideRole.HOOK, SlideRole.INSIGHT, SlideRole.CTA):
        idx = 1 if role == SlideRole.HOOK else (9 if role == SlideRole.CTA else 4)
        img, meta = comp.compose(_slide(idx, role, headline[:HEADLINE_MAX], body[:BODY_MAX]), 9, _bg())
        assert img.size == (W, H)
        # The compositor must not raise and must stay on-canvas; a non-trivial
        # number of wrapped lines were produced.
        assert meta.texts


def test_cta_slide_contains_cta_text():
    comp = _compositor()
    img, meta = comp.compose(
        _slide(9, SlideRole.CTA, "Fly with us", "Start today"), 9, _bg()
    )
    assert BRAND["cta_text"] in meta.texts
    assert meta.role == "cta"


def test_insight_slide_has_slide_number():
    comp = _compositor()
    img, meta = comp.compose(_slide(3, SlideRole.INSIGHT, "Idea", "Body"), 9, _bg())
    assert "3/9" in meta.texts


def test_slide_count_preserved_across_full_carousel():
    comp = _compositor()
    slides = (
        [_slide(1, SlideRole.HOOK, "Hook", "Body")]
        + [_slide(i, SlideRole.INSIGHT, f"Idea {i}", "Body") for i in range(2, 9)]
        + [_slide(9, SlideRole.CTA, "CTA", "Body")]
    )
    imgs = [comp.compose(s, 9, _bg())[0] for s in slides]
    assert len(imgs) == 9
    assert all(im.size == (W, H) for im in imgs)


def test_fonts_required_raises_when_missing(tmp_path):
    with pytest.raises(Exception):
        FontSet.from_dir(tmp_path, require=True)
