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


def _load_bank(path: Path) -> list[str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    concepts = data.get("concepts", [])
    if not isinstance(concepts, list) or not concepts:
        raise TopicEngineError(f"topic bank at {path} has no concepts")
    return [str(c).strip() for c in concepts if str(c).strip()]


def _save_bank(path: Path, concepts: list[str]) -> None:
    path.write_text(
        json.dumps({"concepts": concepts}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


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
        bank = _load_bank(self._bank_path)

        for concept in bank:
            if concept.lower() not in used:
                return concept

        logger.info("topic bank exhausted; requesting fresh concepts from LLM")
        fresh = self._replenish_bank(bank, used)
        for concept in fresh:
            if concept.lower() not in used:
                return concept
        raise TopicEngineError("unable to find an unused concept even after refill")

    def _replenish_bank(self, bank: list[str], used: set[str]) -> list[str]:
        """Ask the LLM for 10 new concepts, keep only genuinely-new ones."""
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

        merged = bank + new_concepts
        _save_bank(self._bank_path, merged)
        logger.info("appended %d new concepts to bank", len(new_concepts))
        return new_concepts

    # -- brief construction --------------------------------------------------
    def build_brief(
        self, run_date: str | None = None, concept_override: str | None = None
    ) -> Brief:
        """Pick (or accept) a concept and return a validated :class:`Brief`.

        ``concept_override`` forces a specific topic (e.g. an admin's typed
        Telegram regenerate request), bypassing bank selection. The aviation
        angle + hook are still generated, and the post is still recorded in the
        dedup log when persisted, so a forced topic cannot silently repeat.
        """
        if concept_override and concept_override.strip():
            concept = concept_override.strip()
            logger.info("using forced concept override: %s", concept)
        else:
            concept = self._next_concept()
        run_date = run_date or date_cls.today().isoformat()
        brief_id = f"{run_date}-{slugify(concept)}"

        audience = self._brand.get("audience", "aspiring pilots / DGCA aspirants")
        tone = self._brand.get("tone", "authoritative but encouraging")

        angle, hook = self._llm_angle_and_hook(concept, audience, tone)

        try:
            return Brief(
                id=brief_id,
                date=run_date,
                concept=concept,
                aviation_angle=angle,
                hook=hook,
                audience=audience,
                tone=tone,
            )
        except ValidationError as exc:
            raise TopicEngineError(f"brief failed validation: {exc}") from exc

    def _llm_angle_and_hook(
        self, concept: str, audience: str, tone: str
    ) -> tuple[str, str]:
        system = (
            f"You are gelio, content strategist for {self._brand.get('name')}, "
            f"an aviation training academy. Brand voice: "
            f"{self._brand.get('voice', tone)}. Audience: {audience}. "
            "All content must be original — no copyrighted text and no real "
            "individuals. Output JSON only."
        )
        user = (
            f"Map the psychology concept '{concept}' onto the lived reality of "
            "aviation / DGCA / pilot-training life. Return JSON with exactly two "
            "keys:\n"
            '  "aviation_angle": one sentence describing the specific pilot-life '
            "angle (e.g. why pilots err at the end of long duty days),\n"
            '  "hook": one scroll-stopping first-line hook (max 90 chars).\n'
            "Make it concrete, fresh, and emotionally resonant for aspiring pilots.\n"
            "Return ONLY valid JSON. Every string value MUST be wrapped in double "
            "quotes and any internal quotes escaped."
        )
        data = self._llm.generate_json(system, user)
        angle = str(data.get("aviation_angle", "")).strip()
        hook = str(data.get("hook", "")).strip()
        if not angle or not hook:
            raise TopicEngineError(
                f"LLM returned incomplete angle/hook for {concept!r}: {data}"
            )
        return angle, hook
