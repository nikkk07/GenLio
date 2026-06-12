"""Content writer: turn a Brief into a fully validated Content artifact.

The LLM is creative inside a fixed schema; the code never trusts its output.
Each attempt is parsed, the ``cta`` from brand.json is injected, the image
prompts are rebuilt around the Brief's consistent ``subject_description``,
unknown panel icons are coerced to ``star``, and series step numbers are
forced from slide order. The result is then validated against both Pydantic
and the business rules in :mod:`gelio.validators`. On any failure the concrete
errors are appended to the next prompt and we retry — up to ``MAX_ATTEMPTS``
times — after which the run aborts without ever writing a bad artifact.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import ValidationError

from gelio.icons import ALLOWED_ICONS, normalize_icon
from gelio.llm import JSONLLM, LLMError
from gelio.schemas import (
    BODY_MAX,
    CAPTION_LIMITS,
    HASHTAGS_MAX,
    HASHTAGS_MIN,
    HEADLINE_LINE_MAX,
    HEADLINE_MAX,
    ITEM_DESC_MAX,
    ITEM_TITLE_MAX,
    SUBHEAD_MAX,
    TIP_MAX,
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

# Composition rules for the right-column photo zone: the subject must live on
# the right third with dark open sky on the left so the content column and the
# gradient mask never fight the photo.
IMAGE_COMPOSITION = (
    "subject on the right third of the frame, looking up or forward, "
    "golden-hour airport background, left side dark open sky"
)

# Used when the Brief carries no subject_description (older briefs).
DEFAULT_SUBJECT = "a young Indian pilot trainee in a crisp white uniform"


class ContentWriterError(RuntimeError):
    """Raised when valid Content cannot be produced within the retry budget."""


def _system_prompt(brand: dict[str, Any], brief: Brief) -> str:
    series_note = ""
    if brief.series_title:
        series_note = (
            f"\nThis carousel is a NUMBERED SERIES titled '{brief.series_title}'. "
            "Each slide is one sequential step of the series in order; the final "
            "slide is the closing step that lands the call to action.\n"
        )
    return (
        f"You are gelio, the content writer for {brand.get('name')}, an aviation "
        f"training academy. Brand voice: {brand.get('voice')}. "
        f"Audience: {brief.audience}. Tone: {brief.tone}. "
        "Write bright, simple, psychologically engaging language. "
        "All content must be 100% original: no copyrighted text, no song/movie "
        "quotes, and no real individuals.\n"
        + series_note
        + "\nEvery slide follows a rich visual design system. Per slide provide:\n"
        f"- `headline_lines`: 2-4 stacked display lines, each {{'text': <= "
        f"{HEADLINE_LINE_MAX} chars, 'color': 'white'|'gold'}}. Bold uppercase "
        "look; make 1-2 lines gold for emphasis. Keep lines punchy (1-3 words "
        "each works best).\n"
        f"- `headline`: the same headline as one flat line (<= {HEADLINE_MAX} "
        "chars, used for captions).\n"
        f"- `subhead`: 1-2 short supporting lines (<= {SUBHEAD_MAX} chars).\n"
        "- `panel`: the slide's content panel. Vary the type across the "
        "carousel: {'type': 'checklist', 'title': short uppercase card title, "
        "'items': [3-5 of {'icon', 'title', 'desc'}]} OR {'type': 'grid4', "
        "'items': [3-4 of {'icon', 'title', 'desc'}]} OR {'type': 'quote', "
        "'quote_lines': [2-3 short bold lines]}. Item titles <= "
        f"{ITEM_TITLE_MAX} chars, descs <= {ITEM_DESC_MAX} chars.\n"
        f"- Panel icons MUST be chosen from exactly this list: "
        f"{sorted(ALLOWED_ICONS)}.\n"
        f"- `tip`: one practical sentence (<= {TIP_MAX} chars) with EXACTLY ONE "
        "key phrase wrapped in *asterisks* for gold emphasis.\n"
        "- `eyebrow`: a 2-4 word uppercase label that is DIFFERENT from the "
        "headline.\n"
        "- `highlight`: 1-2 key words copied VERBATIM from the subhead, to be "
        "emphasised in gold.\n"
        "- `image_prompt`: ONLY a short scene description tied to the slide "
        "topic (e.g. 'standing on the tarmac at dusk beside a small aircraft', "
        "'studying DGCA manuals at a desk by a terminal window', 'in a "
        "commercial cockpit at dawn'). Vary the scene per slide. Do NOT "
        "describe the person and do NOT add style words — the system injects a "
        "consistent subject and brand style automatically.\n"
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
                "headline_lines": [
                    {"text": "YOUR PILOT", "color": "white"},
                    {"text": "DREAM!", "color": "gold"},
                ],
                "headline": f"<= {HEADLINE_MAX} chars (flat version)",
                "subhead": f"<= {SUBHEAD_MAX} chars",
                "highlight": ["keyword", "from subhead"],
                "panel": {
                    "type": "checklist",
                    "title": "WHAT'S INSIDE",
                    "items": [
                        {"icon": "plane", "title": "<= 28 chars", "desc": "<= 70 chars"}
                    ],
                },
                "tip": "One sentence with *one gold phrase* in asterisks.",
                "body": f"<= {BODY_MAX} chars",
                "visual_direction": "short scene description",
                "image_prompt": "scene only, e.g. on the tarmac at golden hour",
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
    if brief.series_title:
        narrative_rules = (
            f"- This is the series '{brief.series_title}': slide N IS step N. "
            f"Slide 1: role 'hook' — step 1, framed as a scroll-stopping series "
            f"opener. Slides 2..{slides - 1}: role 'insight' — steps "
            f"2..{slides - 1} in logical order, each ONE concrete step with a "
            "specific aviation / DGCA / pilot-training detail.\n"
            f"- Slide {slides}: role 'cta' — the FINAL step ({slides}) and the "
            f"closing. headline_lines like [{{'text': \"YOU'RE READY.\", "
            "'color': 'white'}, {'text': 'THE WORLD IS YOUR SKY.', 'color': "
            f"'gold'}}]. It MUST name '{academy}' and carry this call to "
            f"action: \"{cta_text}\".\n"
        )
    else:
        narrative_rules = (
            "- Slide 1: role 'hook' — a curiosity-driven scroll-stopper.\n"
            f"- Slides 2..{slides - 1}: role 'insight' — each delivers ONE "
            "concrete idea with a specific aviation / DGCA / pilot-training "
            "example.\n"
            f"- Slide {slides - 1} (second-to-last): an actionable takeaway the "
            "reader can apply today.\n"
            f"- Slide {slides}: role 'cta' — a closing slide (headline_lines "
            "like [{'text': \"YOU'RE READY.\", 'color': 'white'}, {'text': "
            "'THE WORLD IS YOUR SKY.', 'color': 'gold'}]). It MUST name "
            f"'{academy}' and carry this call to action: \"{cta_text}\".\n"
        )
    return (
        f"Create a {slides}-slide Instagram/LinkedIn carousel.\n\n"
        f"CONCEPT: {brief.concept}\n"
        f"AVIATION ANGLE: {brief.aviation_angle}\n"
        f"HOOK TO BUILD ON: {brief.hook}\n\n"
        "RULES:\n"
        f"- Exactly {slides} slides, indexed 1..{slides} in order.\n"
        + narrative_rules
        + "- EVERY non-cta slide needs headline_lines, subhead, panel and tip "
        "filled in — no sparse slides.\n"
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
        series = bool(brief.series_title)

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
            _apply_image_style(raw, brief)
            _normalize_icons(raw)
            _apply_step_numbers(raw, series=series)

            try:
                content = Content.model_validate(raw)
                validate_content(
                    content,
                    requested_slides=slides,
                    brand=self._brand,
                    series=series,
                )
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


def _apply_image_style(raw: dict[str, Any], brief: Brief) -> None:
    """Rebuild each slide's image_prompt around the consistent subject.

    The LLM supplies only the per-slide scene; the subject description (one
    continuous "character" per carousel), the right-third composition rules
    and the fixed brand style suffix are injected here so composition is never
    left to the model. Mutates ``raw`` in place; tolerant of malformed/partial
    LLM output (the schema validation that follows rejects anything invalid).
    """
    slides = raw.get("slides")
    if not isinstance(slides, list):
        return
    subject = (brief.subject_description or "").strip() or DEFAULT_SUBJECT
    for slide in slides:
        if not isinstance(slide, dict):
            continue
        prompt = slide.get("image_prompt")
        if isinstance(prompt, str) and prompt.strip():
            scene = prompt.strip().rstrip(".,")
            if IMAGE_STYLE_SUFFIX in scene:  # already rebuilt (idempotent)
                slide["image_prompt"] = scene
                continue
            slide["image_prompt"] = (
                f"{subject}, {scene}, {IMAGE_COMPOSITION}, {IMAGE_STYLE_SUFFIX}"
            )


def _normalize_icons(raw: dict[str, Any]) -> None:
    """Coerce unknown panel icon names to ``star`` so icons never fail a run."""
    slides = raw.get("slides")
    if not isinstance(slides, list):
        return
    for slide in slides:
        if not isinstance(slide, dict):
            continue
        panel = slide.get("panel")
        if not isinstance(panel, dict):
            continue
        items = panel.get("items")
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict) and "icon" in item:
                fixed = normalize_icon(str(item.get("icon") or ""))
                if fixed != item.get("icon"):
                    logger.warning(
                        "unknown icon %r coerced to %r", item.get("icon"), fixed
                    )
                item["icon"] = fixed


def _apply_step_numbers(raw: dict[str, Any], *, series: bool) -> None:
    """Force step numbers from slide order (series) or strip them (normal)."""
    slides = raw.get("slides")
    if not isinstance(slides, list):
        return
    for position, slide in enumerate(slides, start=1):
        if not isinstance(slide, dict):
            continue
        if series:
            slide["step_number"] = position
        else:
            slide.pop("step_number", None)


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
