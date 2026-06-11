"""Schema contract tests: field limits and enum integrity."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from gelio.schemas import (
    BODY_MAX,
    HEADLINE_MAX,
    Brief,
    Captions,
    Content,
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


def test_postrecord_defaults_to_drafted():
    rec = PostRecord(id="x", concept="Halo Effect", date="2026-06-11")
    assert rec.state is PostState.DRAFTED
    assert rec.created_at and rec.updated_at


def test_poststate_has_full_lifecycle():
    for name in ["DRAFTED", "AWAITING_APPROVAL", "APPROVED", "COMPLETE", "REJECTED"]:
        assert name in PostState.__members__
