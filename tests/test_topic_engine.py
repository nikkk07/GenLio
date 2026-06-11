"""Topic engine tests: no-repeat selection and bank replenishment (mocked LLM)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gelio.schemas import PostRecord
from gelio.store import Store
from gelio.topic_engine import TopicEngine, slugify
from tests.conftest import BRAND, FakeLLM


def _bank(path: Path, concepts: list[str]) -> Path:
    path.write_text(json.dumps({"concepts": concepts}), encoding="utf-8")
    return path


@pytest.fixture
def store() -> Store:
    s = Store(":memory:")
    yield s
    s.close()


def test_slugify():
    assert slugify("Decision Fatigue") == "decision-fatigue"
    assert slugify("FOMO Effect!") == "fomo-effect"


def test_picks_first_unused_concept(tmp_path, store):
    bank = _bank(tmp_path / "bank.json", ["Alpha", "Beta", "Gamma"])
    engine = TopicEngine(FakeLLM(), store, BRAND, bank)
    brief = engine.build_brief(run_date="2026-06-11")
    assert brief.concept == "Alpha"
    assert brief.id == "2026-06-11-alpha"


def test_excludes_used_concepts(tmp_path, store):
    bank = _bank(tmp_path / "bank.json", ["Alpha", "Beta", "Gamma"])
    store.record_draft(PostRecord(id="x-alpha", concept="Alpha", date="2026-06-10"))
    engine = TopicEngine(FakeLLM(), store, BRAND, bank)
    brief = engine.build_brief(run_date="2026-06-11")
    assert brief.concept == "Beta"


def test_five_runs_no_repeat(tmp_path, store):
    bank = _bank(tmp_path / "bank.json", ["Aa", "Bb", "Cc", "Dd", "Ee"])
    engine = TopicEngine(FakeLLM(), store, BRAND, bank)
    seen = []
    for i in range(5):
        brief = engine.build_brief(run_date="2026-06-11")
        seen.append(brief.concept)
        # simulate persistence so the next run excludes it
        store.record_draft(
            PostRecord(id=f"id-{i}", concept=brief.concept, date="2026-06-11")
        )
    assert sorted(seen) == ["Aa", "Bb", "Cc", "Dd", "Ee"]
    assert len(set(seen)) == 5


def test_replenishes_bank_when_exhausted(tmp_path, store):
    bank = _bank(tmp_path / "bank.json", ["Only"])
    store.record_draft(PostRecord(id="x", concept="Only", date="2026-06-10"))
    llm = FakeLLM(fresh_concepts=["Only", "Brand New One", "Another New"])
    engine = TopicEngine(llm, store, BRAND, bank)

    brief = engine.build_brief(run_date="2026-06-11")
    # "Only" is filtered out as a duplicate; first genuinely-new is chosen.
    assert brief.concept == "Brand New One"

    saved = json.loads(bank.read_text())["concepts"]
    assert "Brand New One" in saved and "Another New" in saved
    assert saved.count("Only") == 1  # duplicate not appended
