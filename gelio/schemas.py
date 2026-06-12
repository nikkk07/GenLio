"""Pydantic data contracts for gelio.

These models are the single source of truth for every artifact that flows
between modules and onto disk. Downstream phases (image generation, approval,
posting) consume the JSON produced by ``Content`` and ``Brief`` unchanged, so
the field names and constraints here are intentionally frozen.

Character limits are enforced at the model boundary: any LLM output that
violates them raises ``pydantic.ValidationError``, which the content writer
catches and feeds back into the next prompt attempt.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from gelio.icons import ALLOWED_ICONS

# --- Field length limits (kept as module constants so validators can reuse) ---
HEADLINE_MAX = 60
BODY_MAX = 220
CAPTION_LIMITS = {"linkedin": 1300, "instagram": 2200, "x": 280}
HASHTAGS_MIN = 5
HASHTAGS_MAX = 10
# Phase 3.7 reference design system limits.
HEADLINE_LINE_MAX = 26
HEADLINE_LINES_MAX = 4
SUBHEAD_MAX = 140
TIP_MAX = 110
PANEL_TITLE_MAX = 32
ITEM_TITLE_MAX = 28
ITEM_DESC_MAX = 70
QUOTE_LINE_MAX = 60
# Panel item-count ranges per panel type (min, max).
PANEL_ITEM_RANGE = {"checklist": (3, 5), "grid4": (3, 4)}
QUOTE_LINES_RANGE = (2, 3)


class SlideRole(str, Enum):
    """Role of a slide within the carousel narrative."""

    HOOK = "hook"
    INSIGHT = "insight"
    CTA = "cta"


class PostState(str, Enum):
    """State machine for a post's lifecycle across all gelio phases.

    Phase 1 only ever reaches :attr:`DRAFTED`, but the full set is defined now
    so later phases never need a schema migration.
    """

    DRAFTED = "DRAFTED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    APPROVED = "APPROVED"
    POSTED_X = "POSTED_X"
    POSTED_IG = "POSTED_IG"
    LINKEDIN_PENDING = "LINKEDIN_PENDING"
    COMPLETE = "COMPLETE"
    REJECTED = "REJECTED"
    FAILED_GENERATION = "FAILED_GENERATION"
    FAILED_VALIDATION = "FAILED_VALIDATION"
    FAILED_POST = "FAILED_POST"
    # Phase 4: per-platform publish failures (the post stays resumable; the
    # per-platform *_status columns are the source of truth for what succeeded).
    FAILED_X = "FAILED_X"
    FAILED_IG = "FAILED_IG"


class Brief(BaseModel):
    """The creative brief produced by the topic engine."""

    model_config = {"extra": "forbid"}

    id: str = Field(..., description="Stable id, e.g. 2026-06-11-decision-fatigue")
    date: str = Field(..., description="ISO date YYYY-MM-DD")
    concept: str = Field(..., min_length=2)
    aviation_angle: str = Field(..., min_length=5)
    hook: str = Field(..., min_length=5)
    audience: str = Field(...)
    tone: str = Field(...)
    # Phase 3.5: short gold eyebrow label for the carousel (additive, optional).
    eyebrow: str | None = None
    # Phase 3.7 (additive/optional): numbered-series title (slides become
    # sequential steps) and one consistent person description reused in every
    # slide's image_prompt so the carousel keeps a continuous "character".
    series_title: str | None = None
    subject_description: str | None = None


class HeadlineLine(BaseModel):
    """One line of the stacked two-tone display headline."""

    model_config = {"extra": "forbid"}

    text: Annotated[str, Field(min_length=1, max_length=HEADLINE_LINE_MAX)]
    color: Literal["white", "gold"] = "white"


class PanelItem(BaseModel):
    """A checklist/grid row: gold icon + bold title + small description."""

    model_config = {"extra": "forbid"}

    icon: str
    title: Annotated[str, Field(min_length=1, max_length=ITEM_TITLE_MAX)]
    desc: Annotated[str, Field(min_length=1, max_length=ITEM_DESC_MAX)]

    @field_validator("icon")
    @classmethod
    def _icon_known(cls, value: str) -> str:
        cleaned = value.strip().lower()
        if cleaned not in ALLOWED_ICONS:
            raise ValueError(f"unknown icon name: {value!r}")
        return cleaned


class Panel(BaseModel):
    """The slide's content panel: checklist card, 3-4 column grid, or quote."""

    model_config = {"extra": "forbid"}

    type: Literal["checklist", "grid4", "quote"]
    title: Annotated[str | None, Field(max_length=PANEL_TITLE_MAX)] = None
    items: list[PanelItem] | None = None
    quote_lines: list[str] | None = None

    @model_validator(mode="after")
    def _shape_matches_type(self) -> "Panel":
        if self.type in PANEL_ITEM_RANGE:
            lo, hi = PANEL_ITEM_RANGE[self.type]
            n = len(self.items or [])
            if not (lo <= n <= hi):
                raise ValueError(f"{self.type} panel needs {lo}-{hi} items, got {n}")
        else:  # quote
            lo, hi = QUOTE_LINES_RANGE
            lines = self.quote_lines or []
            if not (lo <= len(lines) <= hi):
                raise ValueError(
                    f"quote panel needs {lo}-{hi} quote_lines, got {len(lines)}"
                )
            for line in lines:
                if not line.strip() or len(line) > QUOTE_LINE_MAX:
                    raise ValueError(
                        f"quote line must be 1-{QUOTE_LINE_MAX} chars: {line!r}"
                    )
        return self


class Slide(BaseModel):
    """A single carousel slide."""

    model_config = {"extra": "forbid"}

    index: int = Field(..., ge=1)
    role: SlideRole
    headline: Annotated[str, Field(min_length=1, max_length=HEADLINE_MAX)]
    body: Annotated[str, Field(min_length=1, max_length=BODY_MAX)]
    visual_direction: str = Field(..., min_length=3)
    # Phase 3.5 (all additive/optional so pre-3.5 artifacts still validate):
    # photorealistic image-gen prompt, headline keywords to gold-highlight, and a
    # short eyebrow label.
    image_prompt: str | None = None
    highlight: list[str] = Field(default_factory=list)
    eyebrow: str | None = None
    # Phase 3.7 (additive/optional): stacked two-tone display headline, short
    # subhead, icon panel, tip bar, and the series step number. ``headline``
    # stays the flat text used for captions/meta and legacy rendering.
    headline_lines: list[HeadlineLine] = Field(
        default_factory=list, max_length=HEADLINE_LINES_MAX
    )
    subhead: Annotated[str | None, Field(max_length=SUBHEAD_MAX)] = None
    panel: Panel | None = None
    tip: Annotated[str | None, Field(max_length=TIP_MAX)] = None
    step_number: Annotated[int | None, Field(ge=1)] = None


class Captions(BaseModel):
    """Per-platform captions, each bounded by the platform's hard limit."""

    model_config = {"extra": "forbid"}

    linkedin: Annotated[str, Field(min_length=1, max_length=CAPTION_LIMITS["linkedin"])]
    instagram: Annotated[
        str, Field(min_length=1, max_length=CAPTION_LIMITS["instagram"])
    ]
    x: Annotated[str, Field(min_length=1, max_length=CAPTION_LIMITS["x"])]


class Content(BaseModel):
    """The full carousel script + captions produced by the content writer."""

    model_config = {"extra": "forbid"}

    id: str
    slides: list[Slide] = Field(..., min_length=3)
    captions: Captions
    hashtags: list[str] = Field(..., min_length=HASHTAGS_MIN, max_length=HASHTAGS_MAX)
    cta: str = Field(..., min_length=3)

    @field_validator("hashtags")
    @classmethod
    def _hashtags_well_formed(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for tag in value:
            tag = tag.strip()
            if not tag.startswith("#") or " " in tag or len(tag) < 2:
                raise ValueError(f"malformed hashtag: {tag!r}")
            cleaned.append(tag)
        return cleaned


class PostRecord(BaseModel):
    """A persisted post row, mirroring the SQLite ``posts`` table."""

    model_config = {"extra": "forbid"}

    id: str
    concept: str
    date: str
    state: PostState = PostState.DRAFTED
    brief_path: str | None = None
    content_path: str | None = None
    rendered_at: str | None = None  # set by Phase 2 after slides + PDF are built
    regeneration_count: int = 0  # chain depth: original=0, each regen +1 (Phase 3)
    parent_id: str | None = None  # the rejected predecessor a regen was spawned from
    scheduled_time: str | None = None  # ISO timestamp for when to post (Phase 4)
    # Phase 4: per-platform publish tracking. ``*_status`` is None (not
    # attempted), "posted", or "failed"; LinkedIn additionally uses "sent"
    # (PDF delivered to admins, awaiting the Mark-posted tap). The ``*_post_id``
    # columns record the platform-side ids (tweet id, IG media id) for audit.
    x_status: str | None = None
    x_post_id: str | None = None
    ig_status: str | None = None
    ig_media_id: str | None = None
    linkedin_status: str | None = None
    handled_by: str | None = None  # admin chat id that approved/rejected (Phase 4)
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
