"""Topic engine: pick the next unused concept and build a validated Brief.

Responsibilities:
  * Read the concept bank from ``data/topic_bank.json``.
  * Exclude concepts already used (sourced from SQLite via :class:`Store`).
  * If the bank is exhausted, ask the LLM for 10 fresh concepts, validate they
    are genuinely new, append them to the bank file, and continue.
  * For the chosen concept, ask the LLM for an aviation angle + scroll-stopping
    hook, then assemble and validate a :class:`Brief`.

The selection itself is deterministic given the used set (first unused concept
in bank order), which keeps "run it 5 times -> 5 different concepts" provable.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date as date_cls
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from gelio.llm import JSONLLM
from gelio.schemas import Brief
from gelio.store import Store

logger = logging.getLogger("gelio.topic_engine")


class TopicEngineError(RuntimeError):
    """Raised when no concept can be selected or a Brief cannot be built."""


# Bound id slugs so a long admin-typed regenerate topic still yields a compact
# id (keeps filesystem paths sane and Telegram callback_data under its 64-byte
# cap: "<code>|<date>-<slug>" stays well under 64).
MAX_SLUG_LEN = 40


def slugify(text: str, max_len: int = MAX_SLUG_LEN) -> str:
    """Lowercase, hyphenate, strip to ``[a-z0-9-]``, bounded, for stable ids."""
    text = text.strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    if max_len and len(text) > max_len:
        text = text[:max_len].rstrip("-")
    return text


# Used when the LLM omits a subject description; keeps the photo "character"
# consistent across a carousel even on degraded LLM output.
DEFAULT_SUBJECT_DESCRIPTION = "a young Indian pilot trainee in a crisp white uniform"

# Titles like "10 Steps to Become a Pilot in India" are series requests.
_SERIES_TITLE_RE = re.compile(
    r"^\s*(\d{1,2})\s+(steps|ways|rules|habits|lessons|mistakes|signs|stages)\b",
    re.IGNORECASE,
)
SERIES_SLIDES_MIN = 4
SERIES_SLIDES_MAX = 10


def parse_series_request(text: str) -> tuple[str | None, int | None]:
    """Detect a series request in free text (CLI topic or Telegram reply).

    Returns ``(series_title, suggested_slides)``. An explicit ``series:`` prefix
    always wins; otherwise titles shaped like "10 Steps to ..." auto-detect,
    suggesting one slide per step (clamped to a sane carousel size). Plain
    topics return ``(None, None)``.
    """
    cleaned = (text or "").strip()
    if not cleaned:
        return None, None
    if cleaned.lower().startswith("series:"):
        title = cleaned[len("series:"):].strip()
        if not title:
            return None, None
        match = _SERIES_TITLE_RE.match(title)
        count = int(match.group(1)) if match else None
    else:
        match = _SERIES_TITLE_RE.match(cleaned)
        if not match:
            return None, None
        title = cleaned
        count = int(match.group(1))
    if count is not None:
        count = max(SERIES_SLIDES_MIN, min(SERIES_SLIDES_MAX, count))
    return title, count


# Default cross-category weights (favor shareable, curiosity-driven angles).
# Overridable via brand.json "topic_weights".
DEFAULT_WEIGHTS = {
    "myth_busting": 3,
    "career_truth": 3,
    "process": 2,
    "psychology": 2,
}


def _read_bank(path: Path) -> tuple[dict[str, list[str]], str]:
    """Load the topic bank as ordered categories + its on-disk shape.

    Supports both the flat ``{"concepts": [...]}`` shape (-> a single
    ``psychology`` category, shape ``"flat"``) and the categorized
    ``{"categories": {name: [...]}}`` shape (shape ``"categories"``).
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data.get("categories"), dict):
        cats = {
            str(name): [str(c).strip() for c in items if str(c).strip()]
            for name, items in data["categories"].items()
        }
        if not any(cats.values()):
            raise TopicEngineError(f"topic bank at {path} has no concepts")
        return cats, "categories"
    concepts = data.get("concepts", [])
    if not isinstance(concepts, list) or not concepts:
        raise TopicEngineError(f"topic bank at {path} has no concepts")
    return {"psychology": [str(c).strip() for c in concepts if str(c).strip()]}, "flat"


def _save_bank(path: Path, categories: dict[str, list[str]], shape: str) -> None:
    if shape == "categories":
        payload: dict[str, Any] = {"categories": categories}
    else:
        flat: list[str] = []
        for items in categories.values():
            flat.extend(items)
        payload = {"concepts": flat}
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def _weighted_order(
    categories: dict[str, list[str]], weights: dict[str, float]
) -> list[str]:
    """Deterministic weight-proportional interleave across categories.

    A single category returns its concepts in original order (so flat banks keep
    their first-unused-in-order behavior).
    """
    queues = {name: list(items) for name, items in categories.items() if items}
    counters = {name: 0.0 for name in queues}
    order: list[str] = []
    while any(queues.values()):
        live = [n for n in queues if queues[n]]
        # pick the category most "owed" relative to its weight (stable tie-break)
        name = min(live, key=lambda n: counters[n] / max(weights.get(n, 1), 1e-6))
        order.append(queues[name].pop(0))
        counters[name] += 1
    return order


class TopicEngine:
    """Selects concepts and produces Briefs."""

    def __init__(
        self,
        llm: JSONLLM,
        store: Store,
        brand: dict[str, Any],
        topic_bank_path: Path,
    ) -> None:
        self._llm = llm
        self._store = store
        self._brand = brand
        self._bank_path = topic_bank_path

    # -- concept selection ---------------------------------------------------
    def _next_concept(self) -> str:
        used = {c.lower() for c in self._store.used_concepts()}
        categories, shape = _read_bank(self._bank_path)
        weights = {**DEFAULT_WEIGHTS, **self._brand.get("topic_weights", {})}

        for concept in _weighted_order(categories, weights):
            if concept.lower() not in used:
                return concept

        logger.info("topic bank exhausted; requesting fresh concepts from LLM")
        fresh = self._replenish_bank(categories, shape, used)
        for concept in fresh:
            if concept.lower() not in used:
                return concept
        raise TopicEngineError("unable to find an unused concept even after refill")

    def _replenish_bank(
        self, categories: dict[str, list[str]], shape: str, used: set[str]
    ) -> list[str]:
        """Ask the LLM for 10 new concepts, keep only genuinely-new ones."""
        bank = [c for items in categories.values() for c in items]
        existing_lower = {c.lower() for c in bank} | used
        system = (
            "You are a psychology curriculum designer for an aviation training "
            "brand. Output JSON only."
        )
        user = (
            "Propose 10 NEW psychology concepts (cognitive biases, mental models, "
            "or emotional patterns) suitable for short-form content aimed at "
            "Indian aspiring pilots and DGCA aspirants. They must NOT duplicate "
            "any of these existing concepts:\n"
            f"{sorted(bank)}\n\n"
            'Return exactly: {"concepts": ["Name 1", "Name 2", ...]} with 10 '
            "concise title-case names and nothing else."
        )
        data = self._llm.generate_json(system, user)
        proposed = data.get("concepts", [])
        new_concepts = [
            str(c).strip()
            for c in proposed
            if str(c).strip() and str(c).strip().lower() not in existing_lower
        ]
        if not new_concepts:
            raise TopicEngineError("LLM proposed no genuinely new concepts")

        # Append fresh concepts to the psychology category (preserving on-disk shape).
        categories.setdefault("psychology", [])
        categories["psychology"].extend(new_concepts)
        _save_bank(self._bank_path, categories, shape)
        logger.info("appended %d new concepts to bank", len(new_concepts))
        return new_concepts

    # -- brief construction --------------------------------------------------
    def build_brief(
        self,
        run_date: str | None = None,
        concept_override: str | None = None,
        series_title: str | None = None,
    ) -> Brief:
        """Pick (or accept) a concept and return a validated :class:`Brief`.

        ``concept_override`` forces a specific topic (e.g. an admin's typed
        Telegram regenerate request), bypassing bank selection. The aviation
        angle + hook are still generated, and the post is still recorded in the
        dedup log when persisted, so a forced topic cannot silently repeat.
        ``series_title`` makes the carousel a numbered series: the title becomes
        the concept and the content writer turns slides into sequential steps.
        """
        if series_title and series_title.strip():
            concept = series_title.strip()
            logger.info("building series brief: %s", concept)
        elif concept_override and concept_override.strip():
            concept = concept_override.strip()
            logger.info("using forced concept override: %s", concept)
        else:
            concept = self._next_concept()
        run_date = run_date or date_cls.today().isoformat()
        brief_id = f"{run_date}-{slugify(concept)}"

        audience = self._brand.get("audience", "aspiring pilots / DGCA aspirants")
        tone = self._brand.get("tone", "authoritative but encouraging")

        angle, hook, eyebrow, subject = self._llm_angle_and_hook(
            concept, audience, tone
        )

        try:
            return Brief(
                id=brief_id,
                date=run_date,
                concept=concept,
                aviation_angle=angle,
                hook=hook,
                audience=audience,
                tone=tone,
                eyebrow=eyebrow or None,
                series_title=series_title.strip() if series_title else None,
                subject_description=subject or DEFAULT_SUBJECT_DESCRIPTION,
            )
        except ValidationError as exc:
            raise TopicEngineError(f"brief failed validation: {exc}") from exc

    def _llm_angle_and_hook(
        self, concept: str, audience: str, tone: str
    ) -> tuple[str, str, str, str]:
        system = (
            f"You are gelio, content strategist for {self._brand.get('name')}, "
            f"an aviation training academy. Brand voice: "
            f"{self._brand.get('voice', tone)}. Audience: {audience}. "
            "All content must be original — no copyrighted text and no real "
            "individuals. Output JSON only."
        )
        user = (
            f"Frame the topic '{concept}' for the lived reality of aviation / DGCA "
            "/ pilot-training life. Favour a genuinely shareable, curiosity-driven "
            "angle. Return JSON with exactly four keys:\n"
            '  "aviation_angle": one sentence describing the specific pilot-life '
            "angle (e.g. why pilots err at the end of long duty days),\n"
            '  "hook": one scroll-stopping first-line hook (max 90 chars),\n'
            '  "eyebrow": a 2-4 word UPPERCASE label (e.g. "THE REAL CHALLENGE"),\n'
            '  "subject_description": a short description of ONE person to appear '
            "in every AI photo of this carousel (e.g. \"a young Indian woman in a "
            'trainee pilot uniform, early 20s, confident\") — Indian context, no '
            "real individuals.\n"
            "Make it concrete, fresh, and emotionally resonant for aspiring pilots.\n"
            "Return ONLY valid JSON. Every string value MUST be wrapped in double "
            "quotes and any internal quotes escaped."
        )
        data = self._llm.generate_json(system, user)
        angle = str(data.get("aviation_angle", "")).strip()
        hook = str(data.get("hook", "")).strip()
        eyebrow = str(data.get("eyebrow", "")).strip()
        subject = str(data.get("subject_description", "")).strip()
        if not angle or not hook:
            raise TopicEngineError(
                f"LLM returned incomplete angle/hook for {concept!r}: {data}"
            )
        return angle, hook, eyebrow, subject
