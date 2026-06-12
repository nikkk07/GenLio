"""Business-rule validator tests for the Phase 3.7 design structure rules."""

from __future__ import annotations

import pytest

from gelio.schemas import Content
from gelio.validators import ContentValidationError, validate_content
from tests.conftest import BRAND, make_content_dict


def _content(slides=9, mutate=None) -> Content:
    raw = make_content_dict("test-id", slides, BRAND)
    if mutate:
        mutate(raw)
    return Content.model_validate(raw)


def _errors(content, *, slides=9, series=False) -> list[str]:
    try:
        validate_content(
            content, requested_slides=slides, brand=BRAND, series=series
        )
    except ContentValidationError as exc:
        return exc.errors
    return []


def test_full_fixture_passes():
    assert _errors(_content()) == []


def test_missing_panel_rejected_on_content_slide():
    def mutate(raw):
        raw["slides"][2]["panel"] = None

    errors = _errors(_content(mutate=mutate))
    assert any("missing a panel" in e for e in errors)


def test_missing_tip_and_subhead_rejected():
    def mutate(raw):
        raw["slides"][1]["tip"] = None
        raw["slides"][1]["subhead"] = None

    errors = _errors(_content(mutate=mutate))
    assert any("missing a tip" in e for e in errors)
    assert any("missing a subhead" in e for e in errors)


def test_headline_lines_count_enforced():
    def mutate(raw):
        raw["slides"][1]["headline_lines"] = [{"text": "ONLY ONE"}]

    errors = _errors(_content(mutate=mutate))
    assert any("2-4 headline_lines" in e for e in errors)


def test_tip_needs_exactly_one_highlight_phrase():
    def no_phrase(raw):
        raw["slides"][1]["tip"] = "No marked phrase here at all."

    def two_phrases(raw):
        raw["slides"][1]["tip"] = "Both *this* and *that* are marked."

    assert any("exactly one" in e for e in _errors(_content(mutate=no_phrase)))
    assert any("exactly one" in e for e in _errors(_content(mutate=two_phrases)))


def test_cta_slide_exempt_from_panel_tip_rules():
    # The fixture's CTA slide has no panel/tip and must not raise.
    content = _content()
    assert content.slides[-1].panel is None
    assert _errors(content) == []


def test_eyebrow_must_not_duplicate_headline():
    def dup_flat(raw):
        raw["slides"][1]["eyebrow"] = raw["slides"][1]["headline"].upper()

    def dup_lines(raw):
        joined = " ".join(l["text"] for l in raw["slides"][1]["headline_lines"])
        raw["slides"][1]["eyebrow"] = joined.lower()

    assert any("eyebrow duplicates" in e for e in _errors(_content(mutate=dup_flat)))
    assert any("eyebrow duplicates" in e for e in _errors(_content(mutate=dup_lines)))


def test_series_requires_sequential_step_numbers():
    def steps_ok(raw):
        for i, slide in enumerate(raw["slides"], start=1):
            slide["step_number"] = i

    def steps_bad(raw):
        steps_ok(raw)
        raw["slides"][3]["step_number"] = 9

    assert _errors(_content(mutate=steps_ok), series=True) == []
    errors = _errors(_content(mutate=steps_bad), series=True)
    assert any("step_number must be 4" in e for e in errors)


def test_non_series_ignores_step_numbers():
    def steps(raw):
        for i, slide in enumerate(raw["slides"], start=1):
            slide["step_number"] = i

    assert _errors(_content(mutate=steps), series=False) == []
