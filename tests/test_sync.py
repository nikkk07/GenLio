"""Supabase sync tests: payload shape, feature-flag, non-blocking failures."""

from __future__ import annotations

from pathlib import Path

import pytest

from config.settings import Settings
from gelio.schemas import Content, PostRecord, PostState
from gelio.sync import SupabaseSync, build_post_payload
from tests.conftest import BRAND, make_content_dict


def _content() -> Content:
    return Content.model_validate(make_content_dict("2026-06-11-alpha", 9, BRAND))


def _record(**kw) -> PostRecord:
    base = dict(
        id="2026-06-11-alpha",
        concept="Alpha",
        date="2026-06-11",
        state=PostState.APPROVED,
        regeneration_count=2,
        parent_id="2026-06-11-beta",
    )
    base.update(kw)
    return PostRecord(**base)


def _settings(tmp_path: Path, *, url: str = "", key: str = "") -> Settings:
    return Settings(
        provider="groq",
        groq_api_key="",
        groq_model="m",
        gemini_api_key="",
        gemini_model="m",
        brand=BRAND,
        supabase_url=url,
        supabase_service_key=key,
        output_dir=tmp_path,
        db_path=tmp_path / "db.sqlite",
    )


def test_payload_shape():
    payload = build_post_payload(
        _record(), _content(), ["https://x/slide_1.png"], "https://x/c.pdf", "the angle"
    )
    assert payload["id"] == "2026-06-11-alpha"
    assert payload["status"] == "APPROVED"
    assert payload["aviation_angle"] == "the angle"
    assert payload["regeneration_count"] == 2
    assert payload["parent_id"] == "2026-06-11-beta"
    assert payload["slide_urls"] == ["https://x/slide_1.png"]
    assert payload["pdf_url"] == "https://x/c.pdf"
    assert "linkedin" in payload["captions"]
    assert isinstance(payload["hashtags"], list)


def test_disabled_when_url_unset(tmp_path):
    sync = SupabaseSync(_settings(tmp_path))
    assert sync.enabled is False
    # No-ops, no exception.
    sync.push_state(_record())
    sync.push_render(_record(), _content(), [], None)


def test_failure_is_non_blocking(tmp_path):
    sync = SupabaseSync(_settings(tmp_path, url="https://demo.supabase.co", key="svc"))
    assert sync.enabled is True

    def boom(_payload):
        raise RuntimeError("supabase down")

    sync._upsert = boom  # type: ignore[assignment]
    # Must swallow the error, not raise into the caller.
    sync.push_state(_record())


def test_push_render_failure_non_blocking(tmp_path):
    sync = SupabaseSync(_settings(tmp_path, url="https://demo.supabase.co", key="svc"))

    def boom(*a, **k):
        raise RuntimeError("storage down")

    sync.upload_assets = boom  # type: ignore[assignment]
    sync.push_render(_record(), _content(), [], None)  # no raise
