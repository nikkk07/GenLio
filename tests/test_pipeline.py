"""End-to-end pipeline tests with a mocked LLM and temp dirs/db."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import io

from PIL import Image

from config.settings import Settings
from gelio.compositor import Compositor
from gelio.pipeline import Pipeline, Renderer
from gelio.schemas import PostState
from gelio.store import Store
from gelio.visual_gen import GradientFallback, VisualGenerator
from tests.conftest import BRAND, FakeLLM


class _StubShot:
    """Stand-in for Playwright: returns a tiny valid PNG, no browser."""

    def screenshot(self, html, width, height) -> bytes:
        buf = io.BytesIO()
        Image.new("RGB", (16, 20), (0, 0, 0)).save(buf, "PNG")
        return buf.getvalue()


def _fake_visualgen() -> VisualGenerator:
    """A VisualGenerator with no API provider -> always uses the gradient."""
    v = BRAND["visual"]
    fb = GradientFallback(v["blue"], v["navy"], v["gold"])
    return VisualGenerator(providers=[], gradient=fb)


def _fake_renderer(settings: Settings, store: Store) -> Renderer:
    compositor = Compositor(BRAND, screenshotter=_StubShot(), fonts_dir=None, logo_path=None)
    return Renderer(settings, _fake_visualgen(), store, compositor=compositor)


def _settings(tmp_path: Path) -> Settings:
    bank = tmp_path / "topic_bank.json"
    bank.write_text(json.dumps({"concepts": ["Alpha", "Beta", "Gamma"]}))
    return Settings(
        provider="groq",
        groq_api_key="x",
        groq_model="m",
        gemini_api_key="",
        gemini_model="m",
        brand=BRAND,
        root_dir=tmp_path,
        data_dir=tmp_path,
        output_dir=tmp_path / "output",
        db_path=tmp_path / "gelio.db",
        topic_bank_path=bank,
    )


@pytest.fixture
def setup(tmp_path):
    settings = _settings(tmp_path)
    store = Store(settings.db_path)
    pipeline = Pipeline(settings, FakeLLM(slides=9, brand=BRAND), store)
    yield settings, store, pipeline
    store.close()


def test_generate_writes_artifacts_and_drafts(setup):
    settings, store, pipeline = setup
    result = pipeline.generate(slides=9, dry_run=False)

    assert not result.already_existed
    assert result.brief_path.exists()
    assert result.content_path.exists()

    brief_json = json.loads(result.brief_path.read_text())
    content_json = json.loads(result.content_path.read_text())
    assert brief_json["concept"] == "Alpha"
    assert len(content_json["slides"]) == 9

    record = store.get_post(result.brief.id)
    assert record is not None
    assert record.state is PostState.DRAFTED


def test_dry_run_writes_nothing(setup):
    settings, store, pipeline = setup
    result = pipeline.generate(slides=9, dry_run=True)
    assert result.dry_run
    assert not settings.output_dir.exists()
    assert store.get_post(result.brief.id) is None


def test_idempotent_when_id_already_drafted(setup):
    settings, store, pipeline = setup
    # First run drafts a real record on disk + DB.
    first = pipeline.generate(slides=9, dry_run=False)
    assert not first.already_existed

    # Force the engine to re-select the same brief (e.g. same date + concept
    # after a partial earlier run). The pipeline must detect the existing
    # DRAFTED row and not write a duplicate.
    pipeline._engine.build_brief = lambda *a, **k: first.brief  # type: ignore[method-assign]
    second = pipeline.generate(slides=9, dry_run=False)

    assert second.already_existed
    assert second.brief.id == first.brief.id
    ids = [p.id for p in store.all_posts()]
    assert ids.count(first.brief.id) == 1  # no duplicate row


def test_generate_with_render_end_to_end(tmp_path):
    settings = _settings(tmp_path)
    store = Store(settings.db_path)
    renderer = _fake_renderer(settings, store)
    pipeline = Pipeline(settings, FakeLLM(slides=9, brand=BRAND), store, renderer=renderer)
    try:
        result = pipeline.generate(slides=9, dry_run=False, render=True)
    finally:
        pass

    assert result.render is not None
    rr = result.render
    assert not rr.skipped
    assert len(rr.slide_paths) == 9
    assert all(p.exists() for p in rr.slide_paths)
    assert rr.pdf_path.exists()
    # All backgrounds came from the gradient fallback in tests.
    assert set(rr.sources) == {"gradient"}

    # Each slide is exactly 1080x1350.
    for p in rr.slide_paths:
        assert Image.open(p).size == (1080, 1350)

    # rendered_at stamped; state stays DRAFTED.
    rec = store.get_post(result.brief.id)
    assert rec.state is PostState.DRAFTED
    assert rec.rendered_at is not None
    store.close()


def test_render_idempotent_without_force(tmp_path):
    settings = _settings(tmp_path)
    store = Store(settings.db_path)
    renderer = _fake_renderer(settings, store)
    pipeline = Pipeline(settings, FakeLLM(slides=9, brand=BRAND), store, renderer=renderer)
    try:
        first = pipeline.generate(slides=9, dry_run=False, render=True)
        pid = first.brief.id
        mtime_before = first.render.pdf_path.stat().st_mtime_ns

        second = renderer.render(pid, force=False)
        assert second.skipped is True
        assert second.pdf_path.stat().st_mtime_ns == mtime_before

        third = renderer.render(pid, force=True)
        assert third.skipped is False
        assert len(third.slide_paths) == 9
    finally:
        store.close()


def test_five_runs_five_distinct_concepts(tmp_path):
    settings = _settings(tmp_path)
    # widen the bank so five runs are possible
    settings.topic_bank_path.write_text(
        json.dumps({"concepts": ["Aa", "Bb", "Cc", "Dd", "Ee"]})
    )
    store = Store(settings.db_path)
    concepts = []
    try:
        for _ in range(5):
            pipeline = Pipeline(settings, FakeLLM(slides=9, brand=BRAND), store)
            result = pipeline.generate(slides=9, dry_run=False)
            concepts.append(result.brief.concept)
    finally:
        store.close()
    assert len(set(concepts)) == 5
