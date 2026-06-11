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
from typing import Annotated

from pydantic import BaseModel, Field, field_validator

# --- Field length limits (kept as module constants so validators can reuse) ---
HEADLINE_MAX = 60
BODY_MAX = 220
CAPTION_LIMITS = {"linkedin": 1300, "instagram": 2200, "x": 280}
HASHTAGS_MIN = 5
HASHTAGS_MAX = 10


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


class Slide(BaseModel):
    """A single carousel slide."""

    model_config = {"extra": "forbid"}

    index: int = Field(..., ge=1)
    role: SlideRole
    headline: Annotated[str, Field(min_length=1, max_length=HEADLINE_MAX)]
    body: Annotated[str, Field(min_length=1, max_length=BODY_MAX)]
    visual_direction: str = Field(..., min_length=3)


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
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
