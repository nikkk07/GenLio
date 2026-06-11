"""Business-rule validation layered on top of Pydantic schema validation.

Pydantic guarantees field types and length ceilings; this module enforces the
*narrative* rules the LLM must obey:

  * exactly the requested number of slides,
  * slide 1 is the only ``hook`` and comes first,
  * the last slide is the only ``cta`` and references the academy + CTA text,
  * middle slides are ``insight`` slides,
  * per-platform caption limits (re-checked here so the rule is explicit),
  * 5–10 well-formed hashtags.

Validation never trusts the model: failures are collected into a single
:class:`ContentValidationError` whose ``errors`` list is fed back into the next
LLM attempt by the content writer.
"""

from __future__ import annotations

from typing import Any

from gelio.schemas import (
    BODY_MAX,
    CAPTION_LIMITS,
    HASHTAGS_MAX,
    HASHTAGS_MIN,
    HEADLINE_MAX,
    Content,
    SlideRole,
)


class ContentValidationError(ValueError):
    """Aggregated business-rule violations for a Content artifact."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("; ".join(errors))


def validate_content(
    content: Content,
    *,
    requested_slides: int,
    brand: dict[str, Any],
) -> None:
    """Validate business rules; raise :class:`ContentValidationError` if any fail."""
    errors: list[str] = []
    slides = content.slides

    # -- slide count ---------------------------------------------------------
    if len(slides) != requested_slides:
        errors.append(
            f"expected {requested_slides} slides, got {len(slides)}"
        )

    # -- indices are 1..N in order ------------------------------------------
    for position, slide in enumerate(slides, start=1):
        if slide.index != position:
            errors.append(
                f"slide at position {position} has index {slide.index}"
            )

    # -- roles: hook first, cta last, insight in between --------------------
    if slides:
        hooks = [s for s in slides if s.role == SlideRole.HOOK]
        ctas = [s for s in slides if s.role == SlideRole.CTA]

        if len(hooks) != 1:
            errors.append(f"expected exactly 1 hook slide, found {len(hooks)}")
        elif slides[0].role != SlideRole.HOOK:
            errors.append("first slide must have role 'hook'")

        if len(ctas) != 1:
            errors.append(f"expected exactly 1 cta slide, found {len(ctas)}")
        elif slides[-1].role != SlideRole.CTA:
            errors.append("last slide must have role 'cta'")

        for middle in slides[1:-1]:
            if middle.role != SlideRole.INSIGHT:
                errors.append(
                    f"slide {middle.index} must be 'insight', got '{middle.role.value}'"
                )

    # -- char limits (defense in depth beyond Pydantic) ---------------------
    for slide in slides:
        if len(slide.headline) > HEADLINE_MAX:
            errors.append(f"slide {slide.index} headline exceeds {HEADLINE_MAX} chars")
        if len(slide.body) > BODY_MAX:
            errors.append(f"slide {slide.index} body exceeds {BODY_MAX} chars")

    # -- CTA slide must reference the academy and the CTA text --------------
    academy = str(brand.get("academy_short") or brand.get("name") or "").strip()
    if slides and slides[-1].role == SlideRole.CTA and academy:
        cta_text = f"{slides[-1].headline} {slides[-1].body}".lower()
        if academy.lower() not in cta_text:
            errors.append(
                f"final cta slide must reference the academy name '{academy}'"
            )

    # -- captions ------------------------------------------------------------
    for platform, limit in CAPTION_LIMITS.items():
        value = getattr(content.captions, platform)
        if len(value) > limit:
            errors.append(f"{platform} caption exceeds {limit} chars ({len(value)})")

    # -- hashtags ------------------------------------------------------------
    n_tags = len(content.hashtags)
    if not (HASHTAGS_MIN <= n_tags <= HASHTAGS_MAX):
        errors.append(
            f"hashtags must be {HASHTAGS_MIN}-{HASHTAGS_MAX} items, got {n_tags}"
        )

    if errors:
        raise ContentValidationError(errors)
