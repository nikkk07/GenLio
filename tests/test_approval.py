"""Telegram approval-gate tests. No network: a FakeTelegram records every call."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from config.settings import Settings
from gelio.approval import (
    ApprovalService,
    REGEN_PROMPT,
    build_caption_text,
    chunk_list,
    encode_callback,
    parse_regen_marker,
    regen_marker,
    split_text,
)
from gelio.schemas import Content, PostRecord, PostState
from gelio.store import Store
from gelio.sync import SupabaseSync
from gelio.topic_engine import slugify
from tests.conftest import BRAND, make_content_dict

ADMIN = "999"


# --------------------------------------------------------------------------- #
# Fakes & fixtures
# --------------------------------------------------------------------------- #
class FakeTelegram:
    def __init__(self) -> None:
        self.media_groups: list[tuple[list[Path], str | None]] = []
        self.messages: list[tuple[str, dict | None, int]] = []
        self.message_chats: list[tuple[str, str]] = []  # (chat_id, text)
        self.documents: list[tuple[str, Path, str | None]] = []
        self.edits: list[tuple[int, str]] = []
        self.answers: list[tuple[str, str | None]] = []
        self._mid = 100

    def _next(self) -> int:
        self._mid += 1
        return self._mid

    def send_media_group(self, chat_id, photo_paths, caption):
        self.media_groups.append((list(photo_paths), caption))
        return [{"message_id": self._next()}]

    def send_message(self, chat_id, text, reply_markup=None):
        mid = self._next()
        self.messages.append((text, reply_markup, mid))
        self.message_chats.append((str(chat_id), text))
        return {"message_id": mid}

    def send_document(self, chat_id, document_path, caption=None):
        self.documents.append((str(chat_id), document_path, caption))
        return {"message_id": self._next()}

    def edit_message_text(self, chat_id, message_id, text):
        self.edits.append((message_id, text))

    def answer_callback_query(self, callback_query_id, text=None):
        self.answers.append((callback_query_id, text))

    def get_updates(self, offset, timeout):
        return []


def _settings(tmp_path: Path, admin: str = ADMIN) -> Settings:
    return Settings(
        provider="groq",
        groq_api_key="",
        groq_model="m",
        gemini_api_key="",
        gemini_model="m",
        brand=BRAND,
        telegram_bot_token="t",
        telegram_admin_chat_id=admin,
        max_regenerations_per_day=3,
        output_dir=tmp_path / "output",
        db_path=tmp_path / "db.sqlite",
    )


def _seed(
    store: Store,
    settings: Settings,
    post_id: str,
    concept: str,
    date: str,
    state: PostState,
    *,
    n_files: int = 3,
    long_caption: bool = False,
    parent_id: str | None = None,
    rc: int = 0,
) -> None:
    out = settings.output_dir / post_id
    (out / "slides").mkdir(parents=True, exist_ok=True)
    for i in range(1, n_files + 1):
        (out / "slides" / f"slide_{i}.png").write_bytes(b"x")

    content = make_content_dict(post_id, 9, BRAND)
    if long_caption:
        content["captions"]["linkedin"] = "L" * 1200
        content["captions"]["instagram"] = "I" * 800
    (out / "content.json").write_text(json.dumps(content), encoding="utf-8")
    (out / "brief.json").write_text(
        json.dumps({"concept": concept, "hook": "a scroll-stopping hook"}), encoding="utf-8"
    )

    store.record_draft(
        PostRecord(id=post_id, concept=concept, date=date, parent_id=parent_id, regeneration_count=rc)
    )
    if state != PostState.DRAFTED:
        store.transition(post_id, state)


class RegenStub:
    """Stands in for the pipeline: records the call, seeds a rendered post."""

    def __init__(self, store: Store, settings: Settings) -> None:
        self.store = store
        self.settings = settings
        self.calls: list[tuple[str, str | None]] = []

    def __call__(self, parent_id: str, concept_override: str | None) -> str:
        self.calls.append((parent_id, concept_override))
        parent = self.store.get_post(parent_id)
        concept = concept_override or "Auto Picked Topic"
        new_id = f"{parent.date}-{slugify(concept)}"
        _seed(
            self.store,
            self.settings,
            new_id,
            concept,
            parent.date,
            PostState.DRAFTED,
            parent_id=parent_id,
            rc=parent.regeneration_count + 1,
        )
        return new_id


@pytest.fixture
def env(tmp_path):
    settings = _settings(tmp_path)
    store = Store(settings.db_path)
    tg = FakeTelegram()
    sync = SupabaseSync(settings)  # disabled (no url)
    regen = RegenStub(store, settings)
    svc = ApprovalService(tg, store, settings, sync, regen)
    yield settings, store, tg, svc, regen
    store.close()


def _cb(action, post_id, chat_id=ADMIN, cq_id="cq1", message_id=50):
    return {
        "update_id": 1,
        "callback_query": {
            "id": cq_id,
            "data": encode_callback(action, post_id),
            "message": {"message_id": message_id, "chat": {"id": int(chat_id) if str(chat_id).lstrip('-').isdigit() else chat_id}},
        },
    }


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #
def test_chunk_list():
    assert chunk_list(list(range(12)), 10) == [list(range(10)), [10, 11]]


def test_split_text_short():
    assert split_text("hello", 4096) == ["hello"]


def test_split_text_long():
    parts = split_text("\n".join(["line"] * 5000), 100)
    assert all(len(p) <= 100 for p in parts)
    assert len(parts) > 1


def test_regen_marker_roundtrip():
    txt = f"{REGEN_PROMPT}\n\n{regen_marker('2026-06-11-alpha')}"
    assert parse_regen_marker(txt) == "2026-06-11-alpha"


# --------------------------------------------------------------------------- #
# Preview
# --------------------------------------------------------------------------- #
def test_send_preview_transitions_and_buttons(env):
    settings, store, tg, svc, _ = env
    _seed(store, settings, "2026-06-11-alpha", "Alpha", "2026-06-11", PostState.DRAFTED)
    svc.send_preview("2026-06-11-alpha")

    assert store.get_post("2026-06-11-alpha").state is PostState.AWAITING_APPROVAL
    assert len(tg.media_groups) == 1
    # An action message carrying the inline keyboard was sent.
    assert any(rm and "inline_keyboard" in rm for _, rm, _ in tg.messages)


def test_send_preview_media_group_chunking(env):
    settings, store, tg, svc, _ = env
    _seed(store, settings, "2026-06-11-big", "Big", "2026-06-11", PostState.DRAFTED, n_files=12)
    svc.send_preview("2026-06-11-big")
    assert [len(g) for g, _ in tg.media_groups] == [10, 2]


def test_send_preview_caption_overflow_followup(env):
    settings, store, tg, svc, _ = env
    _seed(store, settings, "2026-06-11-long", "Long", "2026-06-11", PostState.DRAFTED, long_caption=True)
    svc.send_preview("2026-06-11-long")
    # Album sent without the long caption ...
    assert tg.media_groups[0][1] is None
    # ... and the captions arrived as a follow-up text message.
    assert any("LinkedIn" in text for text, _, _ in tg.messages)


# --------------------------------------------------------------------------- #
# Callbacks
# --------------------------------------------------------------------------- #
def test_approve_transitions_and_edits(env):
    settings, store, tg, svc, _ = env
    _seed(store, settings, "2026-06-11-alpha", "Alpha", "2026-06-11", PostState.AWAITING_APPROVAL)
    svc.handle_update(_cb("approve", "2026-06-11-alpha"))

    assert store.get_post("2026-06-11-alpha").state is PostState.APPROVED
    assert tg.edits and "Approved" in tg.edits[-1][1]
    assert tg.answers and tg.answers[-1][1] == "Approved ✅"


def test_reject_transitions(env):
    settings, store, tg, svc, _ = env
    _seed(store, settings, "2026-06-11-alpha", "Alpha", "2026-06-11", PostState.AWAITING_APPROVAL)
    svc.handle_update(_cb("reject", "2026-06-11-alpha"))
    assert store.get_post("2026-06-11-alpha").state is PostState.REJECTED
    assert "Rejected" in tg.edits[-1][1]


def test_double_tap_approve_is_noop(env):
    settings, store, tg, svc, _ = env
    _seed(store, settings, "2026-06-11-alpha", "Alpha", "2026-06-11", PostState.AWAITING_APPROVAL)
    svc.handle_update(_cb("approve", "2026-06-11-alpha"))
    edits_after_first = len(tg.edits)
    svc.handle_update(_cb("approve", "2026-06-11-alpha"))  # stale double-tap

    assert store.get_post("2026-06-11-alpha").state is PostState.APPROVED
    assert len(tg.edits) == edits_after_first  # no second edit
    assert tg.answers[-1][1] == f"Already handled by {ADMIN}."


def test_non_admin_callback_ignored(env):
    settings, store, tg, svc, _ = env
    _seed(store, settings, "2026-06-11-alpha", "Alpha", "2026-06-11", PostState.AWAITING_APPROVAL)
    svc.handle_update(_cb("approve", "2026-06-11-alpha", chat_id="111"))
    assert store.get_post("2026-06-11-alpha").state is PostState.AWAITING_APPROVAL
    assert tg.answers == [] and tg.edits == []


# --------------------------------------------------------------------------- #
# Regenerate
# --------------------------------------------------------------------------- #
def _force_reply(parent_id, text, chat_id=ADMIN):
    return {
        "update_id": 2,
        "message": {
            "message_id": 60,
            "chat": {"id": int(chat_id)},
            "text": text,
            "reply_to_message": {"text": f"{REGEN_PROMPT}\n\n{regen_marker(parent_id)}"},
        },
    }


def test_regen_button_sends_forcereply(env):
    settings, store, tg, svc, _ = env
    _seed(store, settings, "2026-06-11-alpha", "Alpha", "2026-06-11", PostState.AWAITING_APPROVAL)
    svc.handle_update(_cb("regen", "2026-06-11-alpha"))
    # A ForceReply prompt embedding the marker was sent.
    assert any(rm and rm.get("force_reply") for _, rm, _ in tg.messages)
    assert any(parse_regen_marker(text) == "2026-06-11-alpha" for text, _, _ in tg.messages)


def test_regen_typed_topic_forces_concept_and_links_parent(env):
    settings, store, tg, svc, regen = env
    _seed(store, settings, "2026-06-11-alpha", "Alpha", "2026-06-11", PostState.AWAITING_APPROVAL)
    svc.handle_update(_force_reply("2026-06-11-alpha", "Fear of Failure"))

    assert regen.calls == [("2026-06-11-alpha", "Fear of Failure")]
    assert store.get_post("2026-06-11-alpha").state is PostState.REJECTED
    child = store.get_post("2026-06-11-fear-of-failure")
    assert child is not None
    assert child.parent_id == "2026-06-11-alpha"
    assert child.regeneration_count == 1
    assert child.state is PostState.AWAITING_APPROVAL  # preview was sent


def test_regen_auto_uses_normal_selection(env):
    settings, store, tg, svc, regen = env
    _seed(store, settings, "2026-06-11-alpha", "Alpha", "2026-06-11", PostState.AWAITING_APPROVAL)
    svc.handle_update(_force_reply("2026-06-11-alpha", "auto"))
    assert regen.calls == [("2026-06-11-alpha", None)]  # no forced concept


def test_fourth_regen_in_a_day_refused(env):
    settings, store, tg, svc, regen = env
    _seed(store, settings, "2026-06-11-alpha", "Alpha", "2026-06-11", PostState.AWAITING_APPROVAL)
    # Three regenerations already exist today (posts with a parent).
    for i in range(3):
        _seed(
            store, settings, f"2026-06-11-regen{i}", f"Regen{i}", "2026-06-11",
            PostState.DRAFTED, parent_id="2026-06-11-alpha", rc=1,
        )
    svc.handle_update(_cb("regen", "2026-06-11-alpha"))

    # No ForceReply prompt; a "max reached" answer instead.
    assert not any(rm and rm.get("force_reply") for _, rm, _ in tg.messages)
    assert "Max regenerations" in tg.answers[-1][1]


def test_caption_builder_contains_all_platforms(env):
    settings, store, tg, svc, _ = env
    content = Content.model_validate(make_content_dict("x", 9, BRAND))
    text = build_caption_text("Decision Fatigue", "the hook", content)
    assert "Decision Fatigue" in text and "the hook" in text
    assert "LinkedIn" in text and "Instagram" in text and "X" in text


# --------------------------------------------------------------------------- #
# Multi-admin (comma-separated TELEGRAM_ADMIN_CHAT_ID)
# --------------------------------------------------------------------------- #
ADMIN2 = "888"


@pytest.fixture
def multi_env(tmp_path):
    settings = _settings(tmp_path, admin=f"{ADMIN}, {ADMIN2}")
    store = Store(settings.db_path)
    tg = FakeTelegram()
    sync = SupabaseSync(settings)  # disabled (no url)
    regen = RegenStub(store, settings)
    published: list[str] = []
    svc = ApprovalService(
        tg, store, settings, sync, regen, publish_runner=published.append
    )
    yield settings, store, tg, svc, published
    store.close()


def test_preview_broadcast_to_all_admins(multi_env):
    settings, store, tg, svc, _ = multi_env
    _seed(store, settings, "2026-06-11-alpha", "Alpha", "2026-06-11", PostState.DRAFTED)
    svc.send_preview("2026-06-11-alpha")

    assert len(tg.media_groups) == 2  # one album per admin
    button_chats = [c for c, t in tg.message_chats if "Choose an action" in t]
    assert sorted(button_chats) == sorted([ADMIN, ADMIN2])


def test_every_admin_is_authorized_for_buttons(multi_env):
    settings, store, tg, svc, _ = multi_env
    _seed(store, settings, "2026-06-11-alpha", "Alpha", "2026-06-11", PostState.AWAITING_APPROVAL)
    svc.handle_update(_cb("approve", "2026-06-11-alpha", chat_id=ADMIN2))
    assert store.get_post("2026-06-11-alpha").state is PostState.APPROVED
    assert store.get_post("2026-06-11-alpha").handled_by == ADMIN2


def test_first_tap_wins_second_admin_sees_who_handled(multi_env):
    settings, store, tg, svc, _ = multi_env
    _seed(store, settings, "2026-06-11-alpha", "Alpha", "2026-06-11", PostState.AWAITING_APPROVAL)
    svc.handle_update(_cb("approve", "2026-06-11-alpha", chat_id=ADMIN))
    svc.handle_update(_cb("reject", "2026-06-11-alpha", chat_id=ADMIN2, cq_id="cq2"))

    assert store.get_post("2026-06-11-alpha").state is PostState.APPROVED  # not rejected
    assert tg.answers[-1] == ("cq2", f"Already handled by {ADMIN}.")


def test_approve_broadcasts_to_other_admins(multi_env):
    settings, store, tg, svc, _ = multi_env
    _seed(store, settings, "2026-06-11-alpha", "Alpha", "2026-06-11", PostState.AWAITING_APPROVAL)
    svc.handle_update(_cb("approve", "2026-06-11-alpha", chat_id=ADMIN))
    other = [t for c, t in tg.message_chats if c == ADMIN2]
    assert any("approved by" in t for t in other)


# --------------------------------------------------------------------------- #
# Immediate publish on Approve (Phase 4 hook)
# --------------------------------------------------------------------------- #
def test_approve_unscheduled_publishes_immediately(multi_env):
    settings, store, tg, svc, published = multi_env
    _seed(store, settings, "2026-06-11-alpha", "Alpha", "2026-06-11", PostState.AWAITING_APPROVAL)
    svc.handle_update(_cb("approve", "2026-06-11-alpha"))
    assert published == ["2026-06-11-alpha"]


def test_approve_scheduled_does_not_publish_immediately(multi_env):
    settings, store, tg, svc, published = multi_env
    _seed(store, settings, "2026-06-11-alpha", "Alpha", "2026-06-11", PostState.AWAITING_APPROVAL)
    store.set_scheduled_time("2026-06-11-alpha", "2099-01-01T00:00:00+00:00")
    svc.handle_update(_cb("approve", "2026-06-11-alpha"))
    assert published == []  # publish-due will pick it up at the scheduled time


# --------------------------------------------------------------------------- #
# Schedule button + typed reply (IST → stored UTC)
# --------------------------------------------------------------------------- #
def _schedule_reply(post_id, text, chat_id=ADMIN):
    from gelio.approval import schedule_marker

    return {
        "update_id": 3,
        "message": {
            "message_id": 70,
            "chat": {"id": int(chat_id)},
            "text": text,
            "reply_to_message": {"text": f"⏰ Send the posting time\n\n{schedule_marker(post_id)}"},
        },
    }


def test_schedule_button_sends_forcereply_prompt(multi_env):
    settings, store, tg, svc, _ = multi_env
    _seed(store, settings, "2026-06-11-alpha", "Alpha", "2026-06-11", PostState.AWAITING_APPROVAL)
    svc.handle_update(_cb("approve_schedule", "2026-06-11-alpha"))
    from gelio.approval import parse_schedule_marker

    assert any(rm and rm.get("force_reply") for _, rm, _ in tg.messages)
    assert any(parse_schedule_marker(t) == "2026-06-11-alpha" for t, _, _ in tg.messages)
    # Not yet approved — the reply carries the decision.
    assert store.get_post("2026-06-11-alpha").state is PostState.AWAITING_APPROVAL


def test_schedule_reply_ist_stores_utc_and_approves(multi_env):
    settings, store, tg, svc, published = multi_env
    _seed(store, settings, "2026-06-11-alpha", "Alpha", "2026-06-11", PostState.AWAITING_APPROVAL)
    svc.handle_update(_schedule_reply("2026-06-11-alpha", "2026-06-13 18:00"))

    record = store.get_post("2026-06-11-alpha")
    assert record.state is PostState.APPROVED
    assert record.scheduled_time == "2026-06-13T12:30:00+00:00"  # 18:00 IST
    assert record.handled_by == ADMIN
    assert published == []  # scheduled, so no immediate publish
    # Confirmation reached both admins.
    chats = {c for c, t in tg.message_chats if "scheduled" in t.lower()}
    assert chats == {ADMIN, ADMIN2}


def test_schedule_reply_explicit_utc_respected(multi_env):
    settings, store, tg, svc, _ = multi_env
    _seed(store, settings, "2026-06-11-alpha", "Alpha", "2026-06-11", PostState.AWAITING_APPROVAL)
    svc.handle_update(_schedule_reply("2026-06-11-alpha", "2026-06-13T12:30:00Z"))
    assert store.get_post("2026-06-11-alpha").scheduled_time == "2026-06-13T12:30:00+00:00"


def test_schedule_reply_invalid_time_keeps_post_pending(multi_env):
    settings, store, tg, svc, _ = multi_env
    _seed(store, settings, "2026-06-11-alpha", "Alpha", "2026-06-11", PostState.AWAITING_APPROVAL)
    svc.handle_update(_schedule_reply("2026-06-11-alpha", "tomorrow evening"))
    record = store.get_post("2026-06-11-alpha")
    assert record.state is PostState.AWAITING_APPROVAL
    assert record.scheduled_time is None
    assert any("Couldn't parse" in t for t, _, _ in tg.messages)


# --------------------------------------------------------------------------- #
# LinkedIn Mark-posted button
# --------------------------------------------------------------------------- #
def test_linkedin_done_marks_posted_and_is_idempotent_across_admins(multi_env):
    settings, store, tg, svc, _ = multi_env
    _seed(store, settings, "2026-06-11-alpha", "Alpha", "2026-06-11", PostState.AWAITING_APPROVAL)
    store.transition("2026-06-11-alpha", PostState.APPROVED)
    store.set_platform_result("2026-06-11-alpha", "linkedin", "sent")
    store.transition("2026-06-11-alpha", PostState.LINKEDIN_PENDING)

    svc.handle_update(_cb("linkedin_done", "2026-06-11-alpha", chat_id=ADMIN))
    assert store.get_post("2026-06-11-alpha").linkedin_status == "posted"
    assert tg.answers[-1][1] == "LinkedIn marked posted ✅"

    svc.handle_update(_cb("linkedin_done", "2026-06-11-alpha", chat_id=ADMIN2, cq_id="cq9"))
    assert tg.answers[-1] == ("cq9", "Already marked posted.")
