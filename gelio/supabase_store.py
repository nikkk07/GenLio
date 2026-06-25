"""Supabase-authoritative :class:`~gelio.store.StateStore` backend.

GitHub Actions runners are ephemeral and the Telegram webhook runs on a
separate serverless function, so local SQLite cannot persist between the daily
cron run and a later approval tap. With ``GELIO_STATE=supabase`` every read and
write goes to Supabase (PostgREST) over ``httpx`` — the authoritative shared
state for the whole $0 architecture.

Behaviour is identical to :class:`~gelio.store.SqliteStore`: the *same*
:func:`~gelio.store.check_transition` rules gate every transition, concept
dedup excludes used concepts, and ``get_posts_due_for_posting`` applies the same
schedule semantics. The on-disk SQLite ``state`` column maps to the PostgREST
``status`` column (the dashboard's existing name); every other field keeps its
name.

Unlike :mod:`gelio.sync` (best-effort, fire-and-forget dashboard mirroring),
this store is *authoritative*: failures raise so a broken Supabase fails the run
loudly rather than silently losing state.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from config.settings import Settings
from gelio.schemas import PostRecord, PostState
from gelio.store import StateStore, StateTransitionError, _now, check_transition

logger = logging.getLogger("gelio.supabase_store")

_TIMEOUT = httpx.Timeout(30.0, connect=10.0)

# Fields PostRecord owns (``extra="forbid"`` means we must never pass unknown
# columns — the dashboard adds captions/slide_urls/etc. that we read past).
_RECORD_FIELDS = set(PostRecord.model_fields)

# The SQLite ``state`` column is the PostgREST ``status`` column.
_STATE_COLUMN = "status"

_net_retry = retry(
    retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    stop=stop_after_attempt(3),
    reraise=True,
)


def record_to_row(record: PostRecord) -> dict[str, Any]:
    """Map a :class:`PostRecord` to a PostgREST ``posts`` row (state→status)."""
    row = record.model_dump(mode="json")
    row[_STATE_COLUMN] = row.pop("state")
    return row


def row_to_record(row: dict[str, Any]) -> PostRecord:
    """Map a PostgREST ``posts`` row back to a :class:`PostRecord`.

    Tolerates extra dashboard columns (captions, slide_urls, …) by keeping only
    the fields PostRecord declares, and reads ``status`` back into ``state``.
    """
    data: dict[str, Any] = {k: v for k, v in row.items() if k in _RECORD_FIELDS}
    if _STATE_COLUMN in row:
        data["state"] = row[_STATE_COLUMN]
    return PostRecord(**data)


class SupabaseStore(StateStore):
    """Authoritative post state, persisted in Supabase via PostgREST."""

    def __init__(
        self,
        url: str,
        service_key: str,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not url or not service_key:
            raise ValueError(
                "SupabaseStore requires SUPABASE_URL and SUPABASE_SERVICE_KEY"
            )
        self._url = url.rstrip("/")
        self._key = service_key
        # Injected in tests (httpx.MockTransport) to run the full state machine
        # against a fake PostgREST without a network.
        self._transport = transport

    @classmethod
    def from_settings(cls, settings: Settings) -> "SupabaseStore":
        return cls(settings.supabase_url, settings.supabase_service_key)

    # -- HTTP plumbing -------------------------------------------------------
    def _headers(self, prefer: str | None = None) -> dict[str, str]:
        headers = {
            "apikey": self._key,
            "Authorization": f"Bearer {self._key}",
            "Content-Type": "application/json",
        }
        if prefer:
            headers["Prefer"] = prefer
        return headers

    @_net_retry
    def _request(
        self,
        method: str,
        table: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        prefer: str | None = None,
    ) -> list[dict[str, Any]]:
        url = f"{self._url}/rest/v1/{table}"
        with httpx.Client(timeout=_TIMEOUT, transport=self._transport) as client:
            resp = client.request(
                method, url, params=params, json=json, headers=self._headers(prefer)
            )
            resp.raise_for_status()
        if resp.status_code == 204 or not resp.content:
            return []
        body = resp.json()
        return body if isinstance(body, list) else [body]

    # -- topic dedup ---------------------------------------------------------
    def used_concepts(self) -> set[str]:
        rows = self._request("GET", "used_concepts", params={"select": "concept"})
        return {r["concept"] for r in rows if r.get("concept")}

    def add_used_concept(self, concept: str) -> None:
        """Idempotently record a concept as used (dedup table)."""
        self._request(
            "POST",
            "used_concepts",
            params={"on_conflict": "concept"},
            json={"concept": concept},
            prefer="resolution=merge-duplicates,return=minimal",
        )

    def count_regenerations(self, date: str) -> int:
        rows = self._request(
            "GET",
            "posts",
            params={
                "select": "id",
                "date": f"eq.{date}",
                "parent_id": "not.is.null",
            },
        )
        return len(rows)

    # -- post records --------------------------------------------------------
    def get_post(self, post_id: str) -> PostRecord | None:
        rows = self._request(
            "GET", "posts", params={"id": f"eq.{post_id}", "select": "*"}
        )
        return row_to_record(rows[0]) if rows else None

    def record_draft(self, record: PostRecord) -> PostRecord:
        self._request(
            "POST",
            "posts",
            json=record_to_row(record),
            prefer="return=minimal",
        )
        # Mirror SQLite: a concept is "used" the moment a row exists.
        self.add_used_concept(record.concept)
        logger.info(
            "recorded draft id=%s concept=%s parent=%s regen=%d scheduled=%s",
            record.id,
            record.concept,
            record.parent_id,
            record.regeneration_count,
            record.scheduled_time,
        )
        return record

    def upsert_record(self, record: PostRecord) -> PostRecord:
        """Full idempotent upsert (used by ``migrate-state``)."""
        self._request(
            "POST",
            "posts",
            params={"on_conflict": "id"},
            json=record_to_row(record),
            prefer="resolution=merge-duplicates,return=minimal",
        )
        self.add_used_concept(record.concept)
        return record

    def _patch(self, post_id: str, changes: dict[str, Any]) -> PostRecord:
        changes = {**changes, "updated_at": _now()}
        rows = self._request(
            "PATCH",
            "posts",
            params={"id": f"eq.{post_id}"},
            json=changes,
            prefer="return=representation",
        )
        if not rows:
            raise StateTransitionError(f"unknown post id {post_id!r}")
        return row_to_record(rows[0])

    def mark_rendered(self, post_id: str) -> PostRecord:
        if self.get_post(post_id) is None:
            raise StateTransitionError(f"unknown post id {post_id!r}")
        updated = self._patch(post_id, {"rendered_at": _now()})
        logger.info("marked rendered id=%s", post_id)
        return updated

    def transition(self, post_id: str, new_state: PostState) -> PostRecord:
        current = self.get_post(post_id)
        if current is None:
            raise StateTransitionError(f"unknown post id {post_id!r}")
        check_transition(post_id, current.state, new_state)
        updated = self._patch(post_id, {_STATE_COLUMN: new_state.value})
        logger.info(
            "transition id=%s %s -> %s", post_id, current.state.value, new_state.value
        )
        return updated

    def all_posts(self) -> list[PostRecord]:
        rows = self._request(
            "GET", "posts", params={"select": "*", "order": "created_at"}
        )
        return [row_to_record(r) for r in rows]

    # -- scheduling ----------------------------------------------------------
    def set_scheduled_time(self, post_id: str, scheduled_time: str) -> PostRecord:
        if self.get_post(post_id) is None:
            raise StateTransitionError(f"unknown post id {post_id!r}")
        updated = self._patch(post_id, {"scheduled_time": scheduled_time})
        logger.info("set scheduled_time id=%s time=%s", post_id, scheduled_time)
        return updated

    def get_posts_due_for_posting(
        self, before_time: str | None = None, *, include_unscheduled: bool = False
    ) -> list[PostRecord]:
        """APPROVED posts whose schedule has passed (+ unscheduled when asked).

        Fetches all APPROVED rows and applies the schedule predicate in Python
        so the semantics are byte-for-byte identical to the SQLite query.
        """
        if before_time is None:
            before_time = _now()
        rows = self._request(
            "GET", "posts", params={_STATE_COLUMN: f"eq.{PostState.APPROVED.value}", "select": "*"}
        )
        records = [row_to_record(r) for r in rows]

        def due(rec: PostRecord) -> bool:
            if rec.scheduled_time is not None:
                return rec.scheduled_time <= before_time
            return include_unscheduled

        out = [r for r in records if due(r)]
        # ORDER BY scheduled_time (NULLs sort last, matching SQLite's ASC nulls).
        out.sort(key=lambda r: (r.scheduled_time is None, r.scheduled_time or ""))
        return out

    # -- per-platform publish tracking ---------------------------------------
    def set_platform_result(
        self, post_id: str, platform: str, status: str, ref_id: str | None = None
    ) -> PostRecord:
        columns = {
            "x": ("x_status", "x_post_id"),
            "ig": ("ig_status", "ig_media_id"),
            "linkedin": ("linkedin_status", None),
        }
        if platform not in columns:
            raise ValueError(f"unknown platform {platform!r}")
        status_col, id_col = columns[platform]
        changes: dict[str, Any] = {status_col: status}
        if id_col is not None and ref_id is not None:
            changes[id_col] = ref_id
        updated = self._patch(post_id, changes)
        logger.info("platform result id=%s %s=%s ref=%s", post_id, platform, status, ref_id)
        return updated

    def set_handled_by(self, post_id: str, admin_chat_id: str) -> None:
        self._patch(post_id, {"handled_by": str(admin_chat_id)})
