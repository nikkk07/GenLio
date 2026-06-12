"""Schema contract tests: field limits and enum integrity."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from gelio.schemas import (
    BODY_MAX,
    HEADLINE_LINE_MAX,
    HEADLINE_MAX,
    ITEM_DESC_MAX,
    ITEM_TITLE_MAX,
    SUBHEAD_MAX,
    TIP_MAX,
    Brief,
    Captions,
    Content,
    HeadlineLine,
    Panel,
    PanelItem,
    PostRecord,
    PostState,
    Slide,
    SlideRole,
)


def _valid_slide(**overrides):
    base = dict(
        index=1,
        role=SlideRole.HOOK,
        headline="A short headline",
        body="A short body.",
        visual_direction="a scene",
    )
    base.update(overrides)
    return Slide(**base)


def test_phase35_fields_are_additive_and_optional():
    # Legacy slide dict (pre-3.5) without the new fields still validates.
    legacy = _valid_slide()
    assert legacy.image_prompt is None
    assert legacy.highlight == []
    assert legacy.eyebrow is None

    # New fields are accepted when present.
    rich = _valid_slide(
        image_prompt="a cinematic cockpit at dawn",
        highlight=["short"],
        eyebrow="THE MYTH",
    )
    assert rich.image_prompt.startswith("a cinematic")
    assert rich.highlight == ["short"]
    assert rich.eyebrow == "THE MYTH"


def test_brief_eyebrow_optional():
    brief = Brief(
        id="2026-06-11-x",
        date="2026-06-11",
        concept="Decision Fatigue",
        aviation_angle="why pilots err late",
        hook="the mistake tired pilots make",
        audience="aspiring pilots",
        tone="encouraging",
    )
    assert brief.eyebrow is None


def test_brief_roundtrip():
    brief = Brief(
        id="2026-06-11-decision-fatigue",
        date="2026-06-11",
        concept="Decision Fatigue",
        aviation_angle="Why pilots err late in duty days.",
        hook="The mistake tired pilots are wired to make.",
        audience="aspiring pilots / DGCA aspirants",
        tone="authoritative but encouraging",
    )
    assert brief.id.endswith("decision-fatigue")


def test_slide_headline_limit_enforced():
    with pytest.raises(ValidationError):
        _valid_slide(headline="x" * (HEADLINE_MAX + 1))


def test_slide_body_limit_enforced():
    with pytest.raises(ValidationError):
        _valid_slide(body="x" * (BODY_MAX + 1))


def test_caption_x_limit_enforced():
    with pytest.raises(ValidationError):
        Captions(linkedin="ok", instagram="ok", x="y" * 281)


def test_hashtags_must_be_well_formed():
    captions = Captions(linkedin="a", instagram="b", x="c")
    slides = [
        _valid_slide(),
        _valid_slide(index=2, role=SlideRole.INSIGHT),
        _valid_slide(index=3, role=SlideRole.CTA),
    ]
    with pytest.raises(ValidationError):
        Content(
            id="x",
            slides=slides,
            captions=captions,
            hashtags=["aviation", "#ok", "#ok2", "#ok3", "#ok4"],  # missing '#'
            cta="cta text",
        )


def _items(n):
    return [
        PanelItem(icon="star", title=f"Item {i}", desc=f"Desc {i}") for i in range(n)
    ]


def test_panel_variants_validate():
    Panel(type="checklist", title="WHAT YOU'VE BUILT", items=_items(3))
    Panel(type="checklist", items=_items(5))
    Panel(type="grid4", items=_items(4))
    Panel(type="quote", quote_lines=["Line one.", "Line two."])
    Panel(type="quote", quote_lines=["One.", "Two.", "Three."])


def test_panel_item_counts_enforced():
    with pytest.raises(ValidationError):
        Panel(type="checklist", items=_items(2))
    with pytest.raises(ValidationError):
        Panel(type="checklist", items=_items(6))
    with pytest.raises(ValidationError):
        Panel(type="grid4", items=_items(5))
    with pytest.raises(ValidationError):
        Panel(type="quote", quote_lines=["only one"])


def test_bad_icon_name_rejected():
    with pytest.raises(ValidationError):
        PanelItem(icon="rocketship", title="T", desc="D")


def test_panel_field_limits_enforced():
    with pytest.raises(ValidationError):
        PanelItem(icon="star", title="x" * (ITEM_TITLE_MAX + 1), desc="d")
    with pytest.raises(ValidationError):
        PanelItem(icon="star", title="t", desc="x" * (ITEM_DESC_MAX + 1))
    with pytest.raises(ValidationError):
        Panel(type="quote", quote_lines=["ok", "x" * 61])


def test_headline_line_limits_enforced():
    HeadlineLine(text="x" * HEADLINE_LINE_MAX, color="gold")
    with pytest.raises(ValidationError):
        HeadlineLine(text="x" * (HEADLINE_LINE_MAX + 1))
    with pytest.raises(ValidationError):
        HeadlineLine(text="ok", color="silver")


def test_slide_new_fields_additive_and_bounded():
    slide = _valid_slide(
        headline_lines=[{"text": "YOUR PILOT"}, {"text": "DREAM", "color": "gold"}],
        subhead="A supporting line.",
        panel={"type": "quote", "quote_lines": ["A.", "B."]},
        tip="Do *this one thing* daily.",
        step_number=3,
    )
    assert slide.headline_lines[1].color == "gold"
    assert slide.step_number == 3
    with pytest.raises(ValidationError):
        _valid_slide(headline_lines=[{"text": f"L{i}"} for i in range(5)])  # max 4
    with pytest.raises(ValidationError):
        _valid_slide(subhead="x" * (SUBHEAD_MAX + 1))
    with pytest.raises(ValidationError):
        _valid_slide(tip="x" * (TIP_MAX + 1))


def test_brief_series_fields_optional():
    brief = Brief(
        id="2026-06-12-x",
        date="2026-06-12",
        concept="10 Steps to Become a Pilot in India",
        aviation_angle="step-by-step pathway",
        hook="here is the map",
        audience="aspiring pilots",
        tone="encouraging",
        series_title="10 Steps to Become a Pilot in India",
        subject_description="a young Indian pilot trainee",
    )
    assert brief.series_title.startswith("10 Steps")
    assert brief.subject_description


def test_postrecord_defaults_to_drafted():
    rec = PostRecord(id="x", concept="Halo Effect", date="2026-06-11")
    assert rec.state is PostState.DRAFTED
    assert rec.created_at and rec.updated_at


def test_poststate_has_full_lifecycle():
    for name in ["DRAFTED", "AWAITING_APPROVAL", "APPROVED", "COMPLETE", "REJECTED"]:
        assert name in PostState.__members__
