"""SupabaseStore state-machine parity tests (HTTP fully mocked).

A small in-memory PostgREST simulator (:class:`_FakePostgrest`, wired in via
``httpx.MockTransport``) lets the real :class:`SupabaseStore` run the entire
state machine with no network. Every behavioural test is parametrized over BOTH
backends — ``sqlite`` and ``supabase`` — so any divergence in transition rules,
dedup, scheduling, or completion semantics fails loudly.
"""

from __future__ import annotations

import json

import httpx
import pytest

from gelio.schemas import PostRecord, PostState
from gelio.store import SqliteStore, StateTransitionError
from gelio.supabase_store import SupabaseStore


# --------------------------------------------------------------------------- #
# In-memory PostgREST simulator
# --------------------------------------------------------------------------- #
class _FakePostgrest:
    """Handles the slice of PostgREST that SupabaseStore actually calls."""

    def __init__(self) -> None:
        self.posts: dict[str, dict] = {}
        self.used_concepts: set[str] = set()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        table = request.url.path.rsplit("/", 1)[-1]
        params = request.url.params
        body = json.loads(request.content) if request.content else None
        prefer = request.headers.get("Prefer", "")
        method = request.method

        if table == "used_concepts":
            return self._used_concepts(method, params, body)
        if table == "posts":
            return self._posts(method, params, body, prefer)
        return httpx.Response(404, json={"message": f"unknown table {table}"})

    # -- used_concepts -------------------------------------------------------
    def _used_concepts(self, method, params, body) -> httpx.Response:
        if method == "GET":
            return httpx.Response(200, json=[{"concept": c} for c in sorted(self.used_concepts)])
        if method == "POST":  # idempotent merge-duplicates upsert
            self.used_concepts.add(body["concept"])
            return httpx.Response(201)
        return httpx.Response(405)

    # -- posts ---------------------------------------------------------------
    def _match(self, row: dict, params) -> bool:
        for key in ("id", "status", "date", "parent_id"):
            if key not in params:
                continue
            spec = params[key]
            if spec == "not.is.null":
                if row.get(key) is None:
                    return False
            elif spec.startswith("eq."):
                if str(row.get(key)) != spec[3:]:
                    return False
        return True

    def _posts(self, method, params, body, prefer) -> httpx.Response:
        if method == "GET":
            rows = [r for r in self.posts.values() if self._match(r, params)]
            order = params.get("order")
            if order:
                rows.sort(key=lambda r: (r.get(order) is None, r.get(order) or ""))
            return httpx.Response(200, json=rows)

        if method == "POST":
            row_id = body["id"]
            exists = row_id in self.posts
            merge = "merge-duplicates" in prefer
            if exists and not merge:
                return httpx.Response(409, json={"message": "duplicate key"})
            self.posts[row_id] = {**self.posts.get(row_id, {}), **body}
            if "return=representation" in prefer:
                return httpx.Response(201, json=[self.posts[row_id]])
            return httpx.Response(201)

        if method == "PATCH":
            updated = []
            for r in self.posts.values():
                if self._match(r, params):
                    r.update(body)
                    updated.append(r)
            return httpx.Response(200, json=updated)

        return httpx.Response(405)


# --------------------------------------------------------------------------- #
# Parametrized backend fixture
# --------------------------------------------------------------------------- #
@pytest.fixture(params=["sqlite", "supabase"])
def store(request):
    if request.param == "sqlite":
        s = SqliteStore(":memory:")
        yield s
        s.close()
    else:
        s = SupabaseStore(
            "https://demo.supabase.co", "svc",
            transport=httpx.MockTransport(_FakePostgrest()),
        )
        yield s


def _draft(store, post_id="2026-06-25-alpha", concept="Alpha", **kw) -> PostRecord:
    rec = PostRecord(id=post_id, concept=concept, date=post_id[:10], **kw)
    return store.record_draft(rec)


# --------------------------------------------------------------------------- #
# Parity tests (run against BOTH backends)
# --------------------------------------------------------------------------- #
def test_record_and_get(store):
    _draft(store)
    got = store.get_post("2026-06-25-alpha")
    assert got is not None
    assert got.concept == "Alpha"
    assert got.state is PostState.DRAFTED


def test_get_missing_is_none(store):
    assert store.get_post("nope") is None


def test_used_concepts_dedup(store):
    _draft(store, "2026-06-25-alpha", "Alpha")
    _draft(store, "2026-06-26-beta", "Beta")
    assert store.used_concepts() == {"Alpha", "Beta"}


def test_legal_transition_chain(store):
    _draft(store)
    store.transition("2026-06-25-alpha", PostState.AWAITING_APPROVAL)
    rec = store.transition("2026-06-25-alpha", PostState.APPROVED)
    assert rec.state is PostState.APPROVED


def test_illegal_transition_raises(store):
    _draft(store)
    # DRAFTED -> APPROVED is not allowed (must pass through AWAITING_APPROVAL).
    with pytest.raises(StateTransitionError):
        store.transition("2026-06-25-alpha", PostState.APPROVED)


def test_transition_unknown_id_raises(store):
    with pytest.raises(StateTransitionError):
        store.transition("ghost", PostState.AWAITING_APPROVAL)


def test_mark_rendered(store):
    _draft(store)
    rec = store.mark_rendered("2026-06-25-alpha")
    assert rec.rendered_at is not None
    assert rec.state is PostState.DRAFTED  # render does not advance state


def test_count_regenerations(store):
    _draft(store, "2026-06-25-alpha", "Alpha")
    _draft(store, "2026-06-25-beta", "Beta", parent_id="2026-06-25-alpha", regeneration_count=1)
    _draft(store, "2026-06-25-gamma", "Gamma", parent_id="2026-06-25-alpha", regeneration_count=2)
    assert store.count_regenerations("2026-06-25") == 2
    assert store.count_regenerations("2026-06-24") == 0


def test_set_scheduled_time(store):
    _draft(store)
    rec = store.set_scheduled_time("2026-06-25-alpha", "2026-06-25T12:30:00+00:00")
    assert rec.scheduled_time == "2026-06-25T12:30:00+00:00"


def _approve(store, post_id):
    store.transition(post_id, PostState.AWAITING_APPROVAL)
    store.transition(post_id, PostState.APPROVED)


def test_due_respects_schedule(store):
    _draft(store, "2026-06-25-past", "Past")
    _draft(store, "2026-06-25-future", "Future")
    _approve(store, "2026-06-25-past")
    _approve(store, "2026-06-25-future")
    store.set_scheduled_time("2026-06-25-past", "2026-06-25T00:00:00+00:00")
    store.set_scheduled_time("2026-06-25-future", "2999-01-01T00:00:00+00:00")

    due = store.get_posts_due_for_posting(before_time="2026-06-25T10:00:00+00:00")
    assert [r.id for r in due] == ["2026-06-25-past"]


def test_due_unscheduled_flag(store):
    _draft(store, "2026-06-25-unsched", "Unsched")
    _approve(store, "2026-06-25-unsched")

    assert store.get_posts_due_for_posting(before_time="2026-06-25T10:00:00+00:00") == []
    due = store.get_posts_due_for_posting(
        before_time="2026-06-25T10:00:00+00:00", include_unscheduled=True
    )
    assert [r.id for r in due] == ["2026-06-25-unsched"]


def test_due_only_approved(store):
    _draft(store, "2026-06-25-draft", "Draft")  # never approved
    assert store.get_posts_due_for_posting(include_unscheduled=True) == []


def test_set_platform_result(store):
    _draft(store)
    _approve(store, "2026-06-25-alpha")
    rec = store.set_platform_result("2026-06-25-alpha", "x", "posted", "tweet123")
    assert rec.x_status == "posted"
    assert rec.x_post_id == "tweet123"


def test_set_handled_by(store):
    _draft(store)
    store.set_handled_by("2026-06-25-alpha", "7215079511")
    assert store.get_post("2026-06-25-alpha").handled_by == "7215079511"


def test_all_posts_ordered(store):
    _draft(store, "2026-06-25-alpha", "Alpha", created_at="2026-06-25T01:00:00+00:00")
    _draft(store, "2026-06-25-beta", "Beta", created_at="2026-06-25T02:00:00+00:00")
    ids = [r.id for r in store.all_posts()]
    assert ids == ["2026-06-25-alpha", "2026-06-25-beta"]


# --------------------------------------------------------------------------- #
# SupabaseStore-specific
# --------------------------------------------------------------------------- #
def test_requires_url_and_key():
    with pytest.raises(ValueError):
        SupabaseStore("", "key")
    with pytest.raises(ValueError):
        SupabaseStore("https://x.supabase.co", "")


def test_upsert_record_is_idempotent():
    store = SupabaseStore(
        "https://demo.supabase.co", "svc",
        transport=httpx.MockTransport(_FakePostgrest()),
    )
    rec = PostRecord(id="2026-06-25-alpha", concept="Alpha", date="2026-06-25")
    store.upsert_record(rec)
    store.upsert_record(rec)  # no 409 on the second write
    assert store.get_post("2026-06-25-alpha") is not None
