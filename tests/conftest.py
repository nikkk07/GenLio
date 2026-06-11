"""Shared test fixtures and fakes (no network access anywhere)."""

from __future__ import annotations

from typing import Any, Callable

import pytest

BRAND: dict[str, Any] = {
    "name": "We One Aviation",
    "academy_short": "We One Aviation",
    "cta_text": "DM We One Aviation today and start your DGCA journey.",
    "tone": "authoritative but encouraging",
    "audience": "aspiring pilots / DGCA aspirants",
    "voice": "Bright, simple, psychologically engaging.",
    "contact": {
        "name": "We One Aviation",
        "email": "info.weoneaviation@gmail.com",
        "phone": "+91-9667370747",
        "address": "C-404, Ramphal Chowk, Dwarka Sector 7, Delhi",
    },
    "visual": {
        "navy": "#0A1F3D",
        "navy_panel": "#0A1A33",
        "blue": "#0B3D91",
        "gold": "#E8B33D",
        "text": "#FFFFFF",
        "muted": "#C9D4E5",
        "slide_size": [1080, 1350],
        "logo_path": "assets/logo.png",
    },
}


def make_content_dict(concept_id: str, slides: int, brand: dict[str, Any]) -> dict[str, Any]:
    """Build a schema-valid Content dict with the academy named in the CTA slide."""
    academy = brand["academy_short"]
    slide_list = [
        {
            "index": 1,
            "role": "hook",
            "eyebrow": "THE REAL CHALLENGE",
            "headline": "The hook that stops the scroll",
            "highlight": ["hook"],
            "body": "A curiosity gap that pulls aspiring pilots in.",
            "visual_direction": "cockpit at dawn, pilot silhouette",
            "image_prompt": "young Indian pilot center-right, open sky on the left",
        }
    ]
    for i in range(2, slides):
        slide_list.append(
            {
                "index": i,
                "role": "insight",
                "eyebrow": f"POINT {i}",
                "headline": f"Insight number {i}",
                "highlight": ["Insight"],
                "body": f"One concrete idea {i} with a DGCA training example.",
                "visual_direction": "training classroom, charts on wall",
                "image_prompt": "student near a small aircraft, sky on the left",
            }
        )
    slide_list.append(
        {
            "index": slides,
            "role": "cta",
            "eyebrow": "READY TO SOAR",
            "headline": f"Fly with {academy}",
            "highlight": [academy],
            "body": f"{academy} mentors you to the cockpit. {brand['cta_text']}",
            "visual_direction": "branded card, runway background",
            "image_prompt": "commercial cockpit at dawn",
        }
    )
    return {
        "id": concept_id,
        "slides": slide_list,
        "captions": {
            "linkedin": "A grounded LinkedIn caption for aspiring pilots.",
            "instagram": "An Instagram caption with energy. ✈️",
            "x": "A tight X post under the limit.",
        },
        "hashtags": [
            "#aviation",
            "#pilottraining",
            "#DGCA",
            "#aspiringpilot",
            "#weoneaviation",
        ],
        "cta": brand["cta_text"],
    }


class FakeLLM:
    """A scripted JSONLLM stand-in.

    Routes ``generate_json`` calls by inspecting the prompt text and returns
    canned dicts. A ``responder`` override lets individual tests script bespoke
    behavior (e.g. invalid-then-valid sequences).
    """

    def __init__(
        self,
        *,
        slides: int = 9,
        brand: dict[str, Any] | None = None,
        responder: Callable[[str, str], dict[str, Any]] | None = None,
        fresh_concepts: list[str] | None = None,
    ) -> None:
        self.slides = slides
        self.brand = brand or BRAND
        self.responder = responder
        self.fresh_concepts = fresh_concepts or [
            "Fresh Concept Alpha",
            "Fresh Concept Beta",
        ]
        self.calls: list[tuple[str, str]] = []

    def generate_json(self, system: str, user: str) -> dict[str, Any]:
        self.calls.append((system, user))
        if self.responder is not None:
            return self.responder(system, user)

        if "Propose 10 NEW" in user:
            return {"concepts": self.fresh_concepts}
        if '"aviation_angle"' in user:
            return {
                "aviation_angle": "Why pilots err at the end of long duty days.",
                "hook": "The mistake every tired pilot is wired to make.",
            }
        if "carousel" in user:
            # id/cta are overwritten by the writer, so a placeholder id is fine.
            return make_content_dict("placeholder-id", self.slides, self.brand)
        raise AssertionError(f"FakeLLM got an unexpected prompt: {user[:80]!r}")


@pytest.fixture
def brand() -> dict[str, Any]:
    return dict(BRAND)
