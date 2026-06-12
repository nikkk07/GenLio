"""Best-effort Supabase sync so a future Next.js dashboard can read gelio state.

Talks to Supabase REST (PostgREST) and Storage directly over ``httpx`` — no
heavy SDK — to keep the dependency footprint small and the calls easy to mock.

Design contract:
  * Feature-flagged: if ``SUPABASE_URL`` is unset the sync is silently disabled
    and every method is a cheap no-op. gelio runs perfectly without it.
  * Non-blocking & best-effort: all network work is wrapped in try/except with
    tenacity retries; a Supabase outage logs a warning and never propagates an
    exception into the Telegram/approval flow.
"""

from __future__ import annotations

import logging
import mimetypes
from pathlib import Path
from typing import Any

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from config.settings import Settings
from gelio.schemas import Content, PostRecord

logger = logging.getLogger("gelio.sync")

_TIMEOUT = httpx.Timeout(30.0, connect=10.0)

_net_retry = retry(
    retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    stop=stop_after_attempt(3),
    reraise=True,
)


def build_post_payload(
    record: PostRecord,
    content: Content | None,
    slide_urls: list[str] | None,
    pdf_url: str | None,
    aviation_angle: str | None = None,
) -> dict[str, Any]:
    """Build the PostgREST row payload for a post (pure, easily testable)."""
    payload: dict[str, Any] = {
        "id": record.id,
        "date": record.date,
        "concept": record.concept,
        "aviation_angle": aviation_angle,
        "status": record.state.value,
        "regeneration_count": record.regeneration_count,
        "parent_id": record.parent_id,
        # Phase 4: schedule + per-platform audit trail (run the matching
        # ALTER TABLE block in supabase/schema.sql once to add the columns).
        "scheduled_time": record.scheduled_time,
        "x_post_id": record.x_post_id,
        "ig_media_id": record.ig_media_id,
        "handled_by": record.handled_by,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }
    if content is not None:
        payload["captions"] = content.captions.model_dump()
        payload["hashtags"] = content.hashtags
    if slide_urls is not None:
        payload["slide_urls"] = slide_urls
    if pdf_url is not None:
        payload["pdf_url"] = pdf_url
    return payload


class SupabaseSync:
    """Pushes post state + assets to Supabase. Safe to call when disabled."""

    def __init__(self, settings: Settings) -> None:
        self._url = settings.supabase_url
        self._key = settings.supabase_service_key
        self._bucket = settings.supabase_bucket
        self.enabled = bool(self._url and self._key)
        if not self.enabled:
            logger.info("supabase sync disabled (SUPABASE_URL unset)")

    # -- headers -------------------------------------------------------------
    def _rest_headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {
            "apikey": self._key,
            "Authorization": f"Bearer {self._key}",
            "Content-Type": "application/json",
        }
        if extra:
            headers.update(extra)
        return headers

    # -- low-level (retried) -------------------------------------------------
    @_net_retry
    def _upsert(self, payload: dict[str, Any]) -> None:
        url = f"{self._url}/rest/v1/posts"
        headers = self._rest_headers(
            {"Prefer": "resolution=merge-duplicates,return=minimal"}
        )
        with httpx.Client(timeout=_TIMEOUT) as client:
            resp = client.post(url, json=payload, headers=headers, params={"on_conflict": "id"})
            resp.raise_for_status()

    @_net_retry
    def _upload(self, object_path: str, data: bytes, content_type: str) -> str:
        url = f"{self._url}/storage/v1/object/{self._bucket}/{object_path}"
        headers = {
            "apikey": self._key,
            "Authorization": f"Bearer {self._key}",
            "Content-Type": content_type,
            "x-upsert": "true",
        }
        with httpx.Client(timeout=_TIMEOUT) as client:
            resp = client.post(url, content=data, headers=headers)
            resp.raise_for_status()
        return f"{self._url}/storage/v1/object/public/{self._bucket}/{object_path}"

    def upload_assets(
        self, post_id: str, slide_paths: list[Path], pdf_path: Path | None
    ) -> dict[str, Any]:
        """Upload slides + pdf to the public bucket; return their public URLs."""
        slide_urls: list[str] = []
        for p in slide_paths:
            ctype = mimetypes.guess_type(p.name)[0] or "image/png"
            slide_urls.append(self._upload(f"{post_id}/{p.name}", p.read_bytes(), ctype))
        pdf_url = None
        if pdf_path is not None and Path(pdf_path).exists():
            pdf_url = self._upload(
                f"{post_id}/{Path(pdf_path).name}", Path(pdf_path).read_bytes(), "application/pdf"
            )
        return {"slide_urls": slide_urls, "pdf_url": pdf_url}

    # -- high-level, best-effort (never raise) -------------------------------
    def push_render(
        self,
        record: PostRecord,
        content: Content,
        slide_paths: list[Path],
        pdf_path: Path | None,
        aviation_angle: str | None = None,
    ) -> None:
        """After render: upload assets and upsert the full row."""
        if not self.enabled:
            return
        try:
            assets = self.upload_assets(record.id, slide_paths, pdf_path)
            payload = build_post_payload(
                record, content, assets["slide_urls"], assets["pdf_url"], aviation_angle
            )
            self._upsert(payload)
            logger.info("supabase push_render ok id=%s", record.id)
        except Exception as exc:  # noqa: BLE001 - best-effort, never block gelio
            logger.warning("supabase push_render failed id=%s: %s", record.id, exc)

    def push_state(self, record: PostRecord, content: Content | None = None) -> None:
        """After a state change: upsert the (lightweight) row, no re-upload."""
        if not self.enabled:
            return
        try:
            payload = build_post_payload(record, content, None, None)
            self._upsert(payload)
            logger.info("supabase push_state ok id=%s status=%s", record.id, record.state.value)
        except Exception as exc:  # noqa: BLE001
            logger.warning("supabase push_state failed id=%s: %s", record.id, exc)


def build_sync(settings: Settings) -> SupabaseSync:
    return SupabaseSync(settings)
