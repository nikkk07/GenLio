"""Content writer: turn a Brief into a fully validated Content artifact.

The LLM is creative inside a fixed schema; the code never trusts its output.
Each attempt is parsed, the ``cta`` from brand.json is injected, then the
result is validated against both Pydantic and the business rules in
:mod:`gelio.validators`. On any failure the concrete errors are appended to the
next prompt and we retry — up to ``MAX_ATTEMPTS`` times — after which the run
aborts without ever writing a bad artifact.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import ValidationError

from gelio.llm import JSONLLM, LLMError
from gelio.schemas import (
    BODY_MAX,
    CAPTION_LIMITS,
    HASHTAGS_MAX,
    HASHTAGS_MIN,
    HEADLINE_MAX,
    Brief,
    Content,
)
from gelio.validators import ContentValidationError, validate_content

logger = logging.getLogger("gelio.content_writer")

MAX_ATTEMPTS = 3

# Fixed style suffix appended to every image_prompt so the photographic look
# stays consistent across days. "no text/letters/logo" suppresses stray AI text.
IMAGE_STYLE_SUFFIX = (
    "cinematic photography, golden hour, shallow depth of field, navy and gold "
    "tone, high detail, no text, no watermark, no logo"
)


class ContentWriterError(RuntimeError):
    """Raised when valid Content cannot be produced within the retry budget."""


def _system_prompt(brand: dict[str, Any], brief: Brief) -> str:
    return (
        f"You are gelio, the content writer for {brand.get('name')}, an aviation "
        f"training academy. Brand voice: {brand.get('voice')}. "
        f"Audience: {brief.audience}. Tone: {brief.tone}. "
        "Write bright, simple, psychologically engaging language. "
        "All content must be 100% original: no copyrighted text, no song/movie "
        "quotes, and no real individuals.\n\n"
        "Each slide ALSO needs an `image_prompt` for a photorealistic AI image — "
        "a CINEMATIC AVIATION SCENE relevant to that slide (e.g. a young Indian "
        "pilot in a crisp uniform on the tarmac, a student studying manuals near a "
        "small aircraft, a commercial cockpit at dawn). Composition rule for "
        "automation: place the SUBJECT on the RIGHT or CENTER-RIGHT with open sky / "
        "negative space on the LEFT, so left-aligned text never covers it. Use "
        "Indian context wherever people appear. Do NOT append style words — the "
        "system adds a fixed brand style suffix automatically.\n"
        "Also give `highlight`: 1-2 key words copied VERBATIM from that slide's "
        "headline, to be emphasised in gold. And `eyebrow`: a 2-4 word uppercase "
        "label for the slide.\n"
        "Output JSON only — no prose, no code fences."
    )


def _user_prompt(brand: dict[str, Any], brief: Brief, slides: int) -> str:
    cta_text = brand.get("cta_text", "")
    academy = brand.get("academy_short") or brand.get("name")
    schema_hint = {
        "id": brief.id,
        "slides": [
            {
                "index": 1,
                "role": "hook",
                "eyebrow": "SHORT UPPERCASE LABEL",
                "headline": f"<= {HEADLINE_MAX} chars",
                "highlight": ["keyword", "from headline"],
                "body": f"<= {BODY_MAX} chars",
                "visual_direction": "short scene description",
                "image_prompt": "photorealistic cinematic aviation scene; subject "
                "center-right, open sky on the left",
            }
        ],
        "captions": {
            "linkedin": f"<= {CAPTION_LIMITS['linkedin']} chars",
            "instagram": f"<= {CAPTION_LIMITS['instagram']} chars",
            "x": f"<= {CAPTION_LIMITS['x']} chars",
        },
        "hashtags": ["#aviation", "#pilottraining", "..."],
        "cta": cta_text,
    }
    return (
        f"Create a {slides}-slide Instagram/LinkedIn carousel.\n\n"
        f"CONCEPT: {brief.concept}\n"
        f"AVIATION ANGLE: {brief.aviation_angle}\n"
        f"HOOK TO BUILD ON: {brief.hook}\n\n"
        "RULES:\n"
        f"- Exactly {slides} slides, indexed 1..{slides} in order.\n"
        "- Slide 1: role 'hook' — a curiosity-driven scroll-stopper.\n"
        f"- Slides 2..{slides - 1}: role 'insight' — each delivers ONE concrete "
        "idea with a specific aviation / DGCA / pilot-training example.\n"
        f"- Slide {slides - 1} (second-to-last): an actionable takeaway the reader "
        "can apply today.\n"
        f"- Slide {slides}: role 'cta' — it MUST name '{academy}' and carry this "
        f"call to action: \"{cta_text}\".\n"
        f"- headline <= {HEADLINE_MAX} chars, body <= {BODY_MAX} chars per slide.\n"
        f"- Provide captions for linkedin (<= {CAPTION_LIMITS['linkedin']}), "
        f"instagram (<= {CAPTION_LIMITS['instagram']}), x (<= {CAPTION_LIMITS['x']}).\n"
        f"- Provide {HASHTAGS_MIN}-{HASHTAGS_MAX} hashtags, each starting with '#', "
        "no spaces.\n\n"
        "Return ONLY valid JSON in EXACTLY this shape (values are placeholders). "
        "Every string value MUST be wrapped in double quotes with internal quotes "
        "escaped; do not emit trailing commas or comments:\n"
        f"{json.dumps(schema_hint, ensure_ascii=False)}"
    )


class ContentWriter:
    """Generates validated :class:`Content` from a :class:`Brief`."""

    def __init__(self, llm: JSONLLM, brand: dict[str, Any]) -> None:
        self._llm = llm
        self._brand = brand

    def write(self, brief: Brief, slides: int) -> Content:
        system = _system_prompt(self._brand, brief)
        base_user = _user_prompt(self._brand, brief, slides)
        cta_text = self._brand.get("cta_text", "")

        feedback = ""
        last_errors: list[str] = []
        for attempt in range(1, MAX_ATTEMPTS + 1):
            user = base_user + feedback
            logger.info("content attempt %d/%d id=%s", attempt, MAX_ATTEMPTS, brief.id)
            try:
                raw = self._llm.generate_json(system, user)
            except LLMError as exc:
                last_errors = [f"llm error: {exc}"]
                feedback = _feedback_block(last_errors)
                continue

            # Force the CTA to come from brand.json — never trust the model here.
            raw["id"] = brief.id
            raw["cta"] = cta_text
            _apply_image_style(raw)

            try:
                content = Content.model_validate(raw)
                validate_content(content, requested_slides=slides, brand=self._brand)
                logger.info("content valid on attempt %d id=%s", attempt, brief.id)
                return content
            except ValidationError as exc:
                last_errors = [_format_pydantic(exc)]
            except ContentValidationError as exc:
                last_errors = exc.errors

            logger.warning(
                "content attempt %d invalid id=%s errors=%s",
                attempt,
                brief.id,
                last_errors,
            )
            feedback = _feedback_block(last_errors)

        raise ContentWriterError(
            f"failed to produce valid content for {brief.id} after "
            f"{MAX_ATTEMPTS} attempts; last errors: {last_errors}"
        )


def _apply_image_style(raw: dict[str, Any]) -> None:
    """Append the fixed brand style suffix to each slide's image_prompt.

    Mutates ``raw`` in place; tolerant of malformed/partial LLM output (the
    schema validation that follows will reject anything still invalid).
    """
    slides = raw.get("slides")
    if not isinstance(slides, list):
        return
    for slide in slides:
        if not isinstance(slide, dict):
            continue
        prompt = slide.get("image_prompt")
        if isinstance(prompt, str) and prompt.strip():
            base = prompt.strip().rstrip(".,")
            if IMAGE_STYLE_SUFFIX not in base:
                slide["image_prompt"] = f"{base}, {IMAGE_STYLE_SUFFIX}"


def _feedback_block(errors: list[str]) -> str:
    bullets = "\n".join(f"- {e}" for e in errors)
    return (
        "\n\nYOUR PREVIOUS RESPONSE WAS REJECTED. Fix these problems and return "
        "corrected JSON only:\n" + bullets
    )


def _format_pydantic(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"])
        parts.append(f"{loc}: {err['msg']}")
    return "schema error -> " + "; ".join(parts)
