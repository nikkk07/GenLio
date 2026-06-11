"""Content writer tests: slide count, limits, CTA slide, retry-on-invalid."""

from __future__ import annotations

import pytest

from gelio.content_writer import ContentWriter, ContentWriterError
from gelio.schemas import Brief, SlideRole
from tests.conftest import BRAND, FakeLLM, make_content_dict


def _brief() -> Brief:
    return Brief(
        id="2026-06-11-decision-fatigue",
        date="2026-06-11",
        concept="Decision Fatigue",
        aviation_angle="Why pilots err late in duty days.",
        hook="The mistake tired pilots are wired to make.",
        audience=BRAND["audience"],
        tone=BRAND["tone"],
    )


def test_writes_valid_content_with_requested_slides():
    writer = ContentWriter(FakeLLM(slides=9), BRAND)
    content = writer.write(_brief(), slides=9)
    assert len(content.slides) == 9
    assert content.slides[0].role is SlideRole.HOOK
    assert content.slides[-1].role is SlideRole.CTA
    assert all(s.role is SlideRole.INSIGHT for s in content.slides[1:-1])


def test_cta_slide_references_academy():
    writer = ContentWriter(FakeLLM(slides=8), BRAND)
    content = writer.write(_brief(), slides=8)
    last = content.slides[-1]
    assert BRAND["academy_short"].lower() in f"{last.headline} {last.body}".lower()


def test_cta_is_forced_from_brand():
    writer = ContentWriter(FakeLLM(slides=9), BRAND)
    content = writer.write(_brief(), slides=9)
    assert content.cta == BRAND["cta_text"]


def test_char_limits_respected():
    writer = ContentWriter(FakeLLM(slides=9), BRAND)
    content = writer.write(_brief(), slides=9)
    for slide in content.slides:
        assert len(slide.headline) <= 60
        assert len(slide.body) <= 220
    assert len(content.captions.x) <= 280


def test_retries_then_succeeds_on_invalid_first_response():
    attempts = {"n": 0}

    def responder(system: str, user: str):
        attempts["n"] += 1
        if attempts["n"] == 1:
            # Wrong slide count -> business-rule rejection, should trigger retry.
            return make_content_dict("placeholder", 5, BRAND)
        return make_content_dict("placeholder", 9, BRAND)

    writer = ContentWriter(FakeLLM(slides=9, responder=responder), BRAND)
    content = writer.write(_brief(), slides=9)
    assert attempts["n"] == 2
    assert len(content.slides) == 9


def test_aborts_after_three_invalid_attempts():
    def responder(system: str, user: str):
        return make_content_dict("placeholder", 4, BRAND)  # always wrong count

    writer = ContentWriter(FakeLLM(slides=9, responder=responder), BRAND)
    with pytest.raises(ContentWriterError):
        writer.write(_brief(), slides=9)
