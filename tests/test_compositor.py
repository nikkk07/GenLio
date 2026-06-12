"""compositor tests: HTML generation + stub screenshotter (no browser/network)."""

from __future__ import annotations

import io

import pytest
from PIL import Image

from gelio.compositor import (
    Compositor,
    headline_font_px,
    highlight_headline,
    synthesize_lines,
    tip_html,
)
from gelio.schemas import (
    BODY_MAX,
    HEADLINE_MAX,
    HeadlineLine,
    Panel,
    PanelItem,
    Slide,
    SlideRole,
)
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


def _compositor(logo_path=None, brand=None) -> tuple[Compositor, StubShot]:
    shot = StubShot()
    return (
        Compositor(brand or BRAND, screenshotter=shot, fonts_dir=None, logo_path=logo_path),
        shot,
    )


def _panel(ptype="checklist"):
    if ptype == "quote":
        return Panel(type="quote", quote_lines=["Line one.", "Line two gold."])
    items = [
        PanelItem(icon="book", title="Ground school", desc="DGCA subjects"),
        PanelItem(icon="plane", title="Flight hours", desc="Logged and signed"),
        PanelItem(icon="medal", title="Medical", desc="Class 2 then Class 1"),
    ]
    return Panel(type=ptype, title="WHAT YOU'VE BUILT", items=items)


def _slide(index, role, headline="Headline here", body="Body text.", highlight=None,
           eyebrow="LABEL", **extra):
    return Slide(
        index=index,
        role=role,
        headline=headline,
        body=body,
        visual_direction="a scene",
        highlight=highlight or [],
        eyebrow=eyebrow,
        **extra,
    )


def _rich_slide(index=2, role=SlideRole.INSIGHT, ptype="checklist", **extra):
    return _slide(
        index,
        role,
        headline_lines=[
            HeadlineLine(text="BUILD YOUR", color="white"),
            HeadlineLine(text="PILOT DREAM", color="gold"),
        ],
        subhead="A short supporting line with a keyword inside.",
        highlight=["keyword"],
        panel=_panel(ptype),
        tip="Review *one DGCA subject* every single day.",
        **extra,
    )


# --------------------------- pure helpers ---------------------------------- #
def test_highlight_wraps_correct_words_in_gold():
    html = highlight_headline("Only rich people fly", ["rich"])
    assert '<span class="gold">rich</span>' in html
    assert "Only " in html and " people fly" in html


def test_highlight_escapes_html():
    html = highlight_headline("A <b> & C", [])
    assert "&lt;b&gt;" in html and "&amp;" in html


def test_tip_html_escapes_then_golds_single_phrase():
    out = tip_html("Watch the *<b>fuel</b> check* daily")
    assert '<span class="gold">&lt;b&gt;fuel&lt;/b&gt; check</span>' in out
    assert "<b>" not in out


def test_synthesize_lines_wraps_and_golds():
    lines = synthesize_lines("Only rich people can become pilots", ["pilots"])
    assert 2 <= len(lines) <= 3
    assert any(l["color"] == "gold" for l in lines)
    assert " ".join(l["text"] for l in lines) == "Only rich people can become pilots"


def test_headline_font_px_clamps():
    assert headline_font_px([{"text": "SHORT"}]) == 110
    assert headline_font_px([{"text": "x" * 14}]) == 84
    assert headline_font_px([{"text": "x" * 30}]) == 64
    assert headline_font_px([]) == 110
    # taller stacks scale down for vertical room, floored at 56
    assert headline_font_px([{"text": "AB"}] * 3) == 96
    assert headline_font_px([{"text": "x" * 30}] * 4) == 56


# ------------------------- structure (per partial) -------------------------- #
def test_rich_slide_has_full_reference_structure():
    comp, _ = _compositor()
    html = comp.build_slide_html(_rich_slide(), 9)
    for cls in (
        "logo-block", "tagline", "gold-divider", "step-badge", "eyebrow-chip",
        "stacked-headline", "subhead", "panel--checklist", "tip-bar",
        "swipe-pill", "decor-arc", "decor-dots", "photo-zone", "photo-mask",
    ):
        assert cls in html, f"missing component: {cls}"


def test_step_badge_shows_slide_counter_outside_series():
    comp, _ = _compositor()
    html = comp.build_slide_html(_rich_slide(index=3), 9)
    assert ">SLIDE<" in html.replace('class="sb-label"', "").replace(
        '<span  >', "<span>"
    ) or "SLIDE" in html
    assert 'class="sb-num">3<' in html


def test_step_badge_shows_step_number_in_series():
    comp, _ = _compositor()
    html = comp.build_slide_html(_rich_slide(index=4, step_number=4), 10)
    assert "STEP" in html
    assert 'class="sb-num">4<' in html


def test_panel_variants_render():
    comp, _ = _compositor()
    for ptype, cls in (
        ("checklist", "panel--checklist"),
        ("grid4", "panel--grid4"),
        ("quote", "panel--quote"),
    ):
        html = comp.build_slide_html(_rich_slide(ptype=ptype), 9)
        assert cls in html
    quote = comp.build_slide_html(_rich_slide(ptype="quote"), 9)
    assert "quote-glyph" in quote and "quote-plane" in quote
    assert 'quote-line gold">Line two gold.' in quote


def test_panel_icons_render_inline_svg():
    comp, _ = _compositor()
    html = comp.build_slide_html(_rich_slide(), 9)
    assert "<svg" in html and "currentColor" in html


def test_tip_bar_renders_gold_phrase():
    comp, _ = _compositor()
    html = comp.build_slide_html(_rich_slide(), 9)
    assert "Tip:" in html
    assert '<span class="gold">one DGCA subject</span>' in html


def test_tagline_rendered_uppercase():
    comp, _ = _compositor()
    html = comp.build_slide_html(_rich_slide(), 9)
    assert "GUIDING ASPIRATIONS, BUILDING CAREERS" in html


def test_hook_has_swipe_cta_shows_dm_pill():
    comp, _ = _compositor()
    hook = comp.build_slide_html(_rich_slide(index=1, role=SlideRole.HOOK), 9)
    cta = comp.build_slide_html(_slide(9, SlideRole.CTA), 9)
    assert "Swipe to continue" in hook
    assert "DM us today" in cta
    assert "Swipe to continue" not in cta


def test_cta_contains_closing_block_and_contact():
    comp, _ = _compositor()
    html = comp.build_slide_html(_slide(9, SlideRole.CTA), 9)
    c = BRAND["contact"]
    assert "closing-block" in html and "wings" in html and "cta-pill" in html
    assert c["email"] in html
    assert c["phone"] in html
    assert c["address"] in html


# ----------------------- sanitization regressions -------------------------- #
def test_headline_lines_escape_html():
    comp, _ = _compositor()
    slide = _rich_slide()
    slide = slide.model_copy(
        update={
            "headline_lines": [
                HeadlineLine(text='<script>"x"&', color="white"),
                HeadlineLine(text="SAFE LINE", color="gold"),
            ]
        }
    )
    html = comp.build_slide_html(slide, 9)
    assert '<script>"x"&' not in html  # the raw payload must never pass through
    assert "&lt;script&gt;&quot;x&quot;&amp;" in html


def test_legacy_slide_without_new_fields_still_renders():
    comp, _ = _compositor()
    html = comp.build_slide_html(
        _slide(2, SlideRole.INSIGHT, "Only rich people fly", highlight=["rich"]), 9
    )
    # Stacked lines synthesized from the flat headline; body in legacy card.
    assert "stacked-headline" in html
    assert "ONLY RICH" in html.upper()
    assert "legacy-card" in html
    assert "Body text." in html


# ------------------------------- rendering --------------------------------- #
@pytest.mark.parametrize("size", [(1080, 1350), (1080, 1080)])
def test_all_roles_render_to_exact_slide_size(size):
    brand = {**BRAND, "visual": {**BRAND["visual"], "slide_size": list(size)}}
    comp, _ = _compositor(brand=brand)
    for role, idx in [(SlideRole.HOOK, 1), (SlideRole.INSIGHT, 4), (SlideRole.CTA, 9)]:
        slide = _rich_slide(index=idx, role=role) if role != SlideRole.CTA else _slide(idx, role)
        img, meta = comp.compose(slide, 9, _png())
        assert img.size == size
        assert meta.size == size
        assert img.mode == "RGB"


def test_long_content_keeps_overflow_safety_net():
    # The compositor must not raise on max-length text; CSS clamp/wrap handle it.
    comp, shot = _compositor()
    headline = "W" * HEADLINE_MAX
    body = ("word " * (BODY_MAX // 5))[:BODY_MAX]
    slide = _slide(
        1,
        SlideRole.HOOK,
        headline,
        body,
        headline_lines=[
            HeadlineLine(text="W" * 26, color="white"),
            HeadlineLine(text="W" * 26, color="gold"),
            HeadlineLine(text="W" * 26, color="white"),
            HeadlineLine(text="W" * 26, color="gold"),
        ],
        subhead="s" * 140,
        panel=Panel(
            type="checklist",
            title="T" * 32,
            items=[
                PanelItem(icon="star", title="t" * 28, desc="d" * 70)
                for _ in range(5)
            ],
        ),
        tip="x" * 90 + " *gold bit*",
    )
    img, _ = comp.compose(slide, 9, _png())
    assert img.size == (W, H)
    assert "overflow: hidden" in shot.last_html  # content box clips as a safety net


def test_logo_missing_falls_back_to_wordmark():
    comp, _ = _compositor(logo_path=None)
    html = comp.build_slide_html(_rich_slide(index=1, role=SlideRole.HOOK), 9)
    assert "WE ONE AVIATION" in html
    assert 'class="wordmark"' in html


def test_logo_present_embeds_data_uri(tmp_path):
    logo = tmp_path / "logo.png"
    Image.new("RGBA", (10, 10), (0, 0, 0, 0)).save(logo)
    comp, _ = _compositor(logo_path=logo)
    html = comp.build_slide_html(_rich_slide(index=1, role=SlideRole.HOOK), 9)
    assert "data:image/png;base64," in html
    assert 'class="logo"' in html


def test_slide_count_preserved():
    comp, _ = _compositor()
    slides = (
        [_rich_slide(index=1, role=SlideRole.HOOK)]
        + [_rich_slide(index=i) for i in range(2, 9)]
        + [_slide(9, SlideRole.CTA)]
    )
    imgs = [comp.compose(s, 9, _png())[0] for s in slides]
    assert len(imgs) == 9 and all(im.size == (W, H) for im in imgs)


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (12, 15), (12, 31, 61)).save(buf, "PNG")
    return buf.getvalue()
