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


def test_every_content_slide_has_panel_and_tip():
    writer = ContentWriter(FakeLLM(slides=9), BRAND)
    content = writer.write(_brief(), slides=9)
    for slide in content.slides[:-1]:
        assert slide.panel is not None
        assert slide.tip
        assert 2 <= len(slide.headline_lines) <= 4
        assert slide.subhead


def test_unknown_icon_coerced_to_star():
    def responder(system: str, user: str):
        raw = make_content_dict("placeholder", 9, BRAND)
        raw["slides"][1]["panel"] = {
            "type": "checklist",
            "title": "CHECKS",
            "items": [
                {"icon": "rocketship", "title": "Bad icon", "desc": "Coerced"},
                {"icon": "plane", "title": "Good icon", "desc": "Kept"},
                {"icon": "WINGS", "title": "Case fixed", "desc": "Lowercased"},
            ],
        }
        return raw

    writer = ContentWriter(FakeLLM(slides=9, responder=responder), BRAND)
    content = writer.write(_brief(), slides=9)
    icons = [item.icon for item in content.slides[1].panel.items]
    assert icons == ["star", "plane", "wings"]


def test_image_prompts_rebuilt_with_locked_subject_and_negatives():
    from gelio.content_writer import IMAGE_NEGATIVE, LOCKED_SUBJECT

    # The brief's subject_description is deliberately IGNORED: the recurring
    # character is locked so the same face recurs with no drift between slides.
    brief = _brief().model_copy(
        update={"subject_description": "a young Indian woman pilot, early 20s"}
    )
    writer = ContentWriter(FakeLLM(slides=9), BRAND)
    content = writer.write(brief, slides=9)
    prompts = {s.image_prompt for s in content.slides}
    # Every slide carries the byte-identical locked subject + composition + negatives.
    for slide in content.slides:
        assert slide.image_prompt.startswith(LOCKED_SUBJECT)
        assert "a young Indian woman" not in slide.image_prompt  # brief ignored
        assert "right third" in slide.image_prompt
        assert "no watermark" in slide.image_prompt
        assert f"Avoid: {IMAGE_NEGATIVE}" in slide.image_prompt
    # The locked subject prefix is identical on every slide (only the scene varies).
    assert len({p[: len(LOCKED_SUBJECT)] for p in prompts}) == 1


def test_series_step_numbers_forced_from_order():
    brief = _brief().model_copy(
        update={"series_title": "10 Steps to Become a Pilot in India"}
    )

    def responder(system: str, user: str):
        raw = make_content_dict("placeholder", 9, BRAND)
        raw["slides"][4]["step_number"] = 99  # writer must overwrite this
        return raw

    writer = ContentWriter(FakeLLM(slides=9, responder=responder), BRAND)
    content = writer.write(brief, slides=9)
    assert [s.step_number for s in content.slides] == list(range(1, 10))


def test_non_series_strips_step_numbers():
    def responder(system: str, user: str):
        raw = make_content_dict("placeholder", 9, BRAND)
        raw["slides"][2]["step_number"] = 7
        return raw

    writer = ContentWriter(FakeLLM(slides=9, responder=responder), BRAND)
    content = writer.write(_brief(), slides=9)
    assert all(s.step_number is None for s in content.slides)
