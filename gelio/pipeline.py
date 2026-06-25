"""Orchestrates one end-to-end daily run: Brief -> Content -> validated artifacts.

The pipeline is the only module that touches disk + DB together. It guarantees:
  * idempotency — a second run for the same id detects the existing DRAFTED
    record and reports its location instead of duplicating work;
  * artifacts — ``output/<id>/brief.json`` and ``output/<id>/content.json`` are
    written only after full validation succeeds;
  * dry-run — prints the would-be Brief + Content and writes nothing.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from pathlib import Path

from config.settings import Settings, load_settings
from gelio.compositor import Compositor, PlaywrightScreenshotter
from gelio.content_writer import IMAGE_STYLE_SUFFIX, ContentWriter
from gelio.llm import JSONLLM, build_client
from gelio.pdf_builder import build_pdf
from gelio.schemas import Brief, Content, PostRecord, PostState
from gelio.store import StateStore, build_store
from gelio.sync import SupabaseSync, build_sync
from gelio.topic_engine import TopicEngine
from gelio.visual_gen import VisualGenerator, build_generator, slide_seed

logger = logging.getLogger("gelio.pipeline")


@dataclass
class RunResult:
    """Outcome of a pipeline run."""

    brief: Brief
    content: Content
    dry_run: bool
    already_existed: bool = False
    brief_path: Path | None = None
    content_path: Path | None = None
    render: "RenderResult | None" = None


@dataclass
class RenderResult:
    """Outcome of a Phase 2 render."""

    post_id: str
    slide_paths: list[Path]
    pdf_path: Path
    sources: list[str]  # per-slide: "cloudflare" | "together" | "gradient"
    skipped: bool = False
    degraded: bool = False  # too few real AI photos (gradient fallbacks)


# A render is DEGRADED when fewer than this fraction of slides got a real AI
# photo (the reference threshold: >= 7 of 9 slides).
DEGRADED_PHOTO_FRACTION = 7 / 9


def _is_degraded(sources: list[str]) -> bool:
    if not sources:
        return False
    real = sum(1 for s in sources if s not in ("gradient", "fallback"))
    return real < math.ceil(DEGRADED_PHOTO_FRACTION * len(sources))


def _write_json(path: Path, model) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(model.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def load_content(output_dir: Path, post_id: str) -> Content:
    """Load and validate ``output/<id>/content.json`` from disk."""
    path = output_dir / post_id / "content.json"
    if not path.exists():
        raise FileNotFoundError(f"no content artifact at {path}")
    return Content.model_validate(json.loads(path.read_text(encoding="utf-8")))


class Renderer:
    """Phase 2: turn a validated Content artifact into slides + a carousel PDF.

    The compositor (and therefore the brand fonts) is resolved lazily at render
    time, so Phase-1-only runs never require font assets. Tests inject a
    compositor backed by the scalable fallback font.
    """

    def __init__(
        self,
        settings: Settings,
        visual: VisualGenerator,
        store: StateStore,
        compositor: Compositor | None = None,
        sync: SupabaseSync | None = None,
    ) -> None:
        self._settings = settings
        self._visual = visual
        self._store = store
        self._compositor = compositor
        self._owns_compositor = False  # True when we lazily build (and must close) it
        self._sync = sync

    def _brief_angle(self, post_id: str) -> str | None:
        """Best-effort read of the aviation angle from the on-disk brief."""
        path = self._settings.output_dir / post_id / "brief.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8")).get("aviation_angle")
        except Exception:  # noqa: BLE001
            return None

    def _get_compositor(self) -> Compositor:
        if self._compositor is None:
            visual = self._settings.brand.get("visual", {})
            logo = self._settings.root_dir / visual.get("logo_path", "assets/logo.png")
            self._compositor = Compositor(
                self._settings.brand,
                screenshotter=PlaywrightScreenshotter(),
                fonts_dir=self._settings.fonts_dir,
                logo_path=logo if logo.exists() else None,
            )
            self._owns_compositor = True
        return self._compositor

    def render(self, post_id: str, *, force: bool = False) -> RenderResult:
        content = load_content(self._settings.output_dir, post_id)
        out_dir = self._settings.output_dir / post_id
        bg_dir = out_dir / "backgrounds"
        slides_dir = out_dir / "slides"
        pdf_path = out_dir / "carousel.pdf"
        n = len(content.slides)

        slide_paths = [slides_dir / f"slide_{s.index}.png" for s in content.slides]
        complete = pdf_path.exists() and all(p.exists() for p in slide_paths)
        if complete and not force:
            logger.info("render skipped id=%s (slides complete, no --force)", post_id)
            return RenderResult(
                post_id=post_id,
                slide_paths=slide_paths,
                pdf_path=pdf_path,
                sources=["existing"] * n,
                skipped=True,
            )

        bg_dir.mkdir(parents=True, exist_ok=True)
        slides_dir.mkdir(parents=True, exist_ok=True)
        compositor = self._get_compositor()
        visual_cfg = self._settings.brand.get("visual", {})
        width, height = visual_cfg.get("slide_size", [1080, 1350])

        sources: list[str] = []
        try:
            for slide in content.slides:
                prompt = slide.image_prompt or (
                    f"{slide.visual_direction}, {IMAGE_STYLE_SUFFIX}"
                )
                seed = slide_seed(post_id, slide.index)
                bg = self._visual.generate(prompt, width, height, seed)
                (bg_dir / f"slide_{slide.index}.png").write_bytes(bg.data)

                image, _meta = compositor.compose(slide, n, bg.data)
                image.save(slides_dir / f"slide_{slide.index}.png", format="PNG")
                sources.append(bg.source)
                logger.info(
                    "rendered slide id=%s index=%d source=%s",
                    post_id, slide.index, bg.source,
                )
        finally:
            if self._owns_compositor:
                compositor.close()
                self._compositor = None
                self._owns_compositor = False

        build_pdf(slide_paths, pdf_path)

        record = self._store.get_post(post_id)
        if record is not None:
            record = self._store.mark_rendered(post_id)
            if self._sync is not None:
                angle = self._brief_angle(post_id)
                self._sync.push_render(record, content, slide_paths, pdf_path, angle)

        by_source = {s: sources.count(s) for s in sorted(set(sources))}
        degraded = _is_degraded(sources)
        if degraded:
            logger.warning(
                "DEGRADED render id=%s: too few real AI photos sources=%s",
                post_id,
                by_source,
            )
        logger.info("render complete id=%s slides=%d sources=%s", post_id, n, by_source)
        return RenderResult(
            post_id=post_id,
            slide_paths=slide_paths,
            pdf_path=pdf_path,
            sources=sources,
            degraded=degraded,
        )


class Pipeline:
    """Wire the modules together for a single run."""

    def __init__(
        self,
        settings: Settings,
        llm: JSONLLM,
        store: StateStore,
        renderer: Renderer | None = None,
    ) -> None:
        self._settings = settings
        self._store = store
        self._engine = TopicEngine(
            llm=llm,
            store=store,
            brand=settings.brand,
            topic_bank_path=settings.topic_bank_path,
        )
        self._writer = ContentWriter(llm=llm, brand=settings.brand)
        self._renderer = renderer

    def render(self, post_id: str, *, force: bool = False) -> RenderResult:
        if self._renderer is None:
            raise RuntimeError("pipeline was built without a renderer")
        return self._renderer.render(post_id, force=force)

    def generate(
        self,
        slides: int,
        *,
        dry_run: bool = False,
        render: bool = False,
        force: bool = False,
        concept_override: str | None = None,
        series_title: str | None = None,
        parent_id: str | None = None,
        regeneration_count: int = 0,
    ) -> RunResult:
        brief = self._engine.build_brief(
            concept_override=concept_override, series_title=series_title
        )
        logger.info("brief built id=%s concept=%s", brief.id, brief.concept)

        # Idempotency: detect an existing record before doing any writes.
        existing = self._store.get_post(brief.id)
        if existing is not None and not dry_run:
            logger.info("id=%s already exists state=%s", brief.id, existing.state.value)
            # Reuse the on-disk content instead of burning another LLM call.
            content = load_content(self._settings.output_dir, brief.id)
            render_result = (
                self.render(brief.id, force=force) if render else None
            )
            return RunResult(
                brief=brief,
                content=content,
                dry_run=False,
                already_existed=True,
                brief_path=Path(existing.brief_path) if existing.brief_path else None,
                content_path=(
                    Path(existing.content_path) if existing.content_path else None
                ),
                render=render_result,
            )

        content = self._writer.write(brief, slides)
        logger.info("content built id=%s slides=%d", brief.id, len(content.slides))

        if dry_run:
            return RunResult(brief=brief, content=content, dry_run=True)

        out_dir = self._settings.output_dir / brief.id
        brief_path = out_dir / "brief.json"
        content_path = out_dir / "content.json"
        _write_json(brief_path, brief)
        _write_json(content_path, content)

        record = PostRecord(
            id=brief.id,
            concept=brief.concept,
            date=brief.date,
            state=PostState.DRAFTED,
            brief_path=str(brief_path),
            content_path=str(content_path),
            regeneration_count=regeneration_count,
            parent_id=parent_id,
        )
        self._store.record_draft(record)

        render_result = self.render(brief.id, force=force) if render else None

        return RunResult(
            brief=brief,
            content=content,
            dry_run=False,
            brief_path=brief_path,
            content_path=content_path,
            render=render_result,
        )


def build_pipeline(settings: Settings | None = None) -> tuple[Pipeline, StateStore]:
    """Construct a Pipeline with live settings, LLM client, and store.

    Returns the pipeline and the owning store so the caller can close it.
    """
    settings = settings or load_settings()
    store = build_store(settings)
    llm = build_client(settings)
    visual = build_generator(settings)
    sync = build_sync(settings)
    renderer = Renderer(settings, visual, store, sync=sync)
    return Pipeline(settings, llm, store, renderer=renderer), store


def build_renderer(settings: Settings | None = None) -> tuple[Renderer, StateStore]:
    """Construct a standalone Renderer (no LLM) for ``render <id>``.

    Returns the renderer and owning store so the caller can close it.
    """
    settings = settings or load_settings()
    store = build_store(settings)
    visual = build_generator(settings)
    sync = build_sync(settings)
    return Renderer(settings, visual, store, sync=sync), store
