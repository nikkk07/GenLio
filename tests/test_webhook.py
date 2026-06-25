"""Webhook routing + secret-validation tests (no network).

Confirms the FastAPI app rejects spoofed POSTs (bad path/header secret) and
routes a valid update to the SAME ApprovalService.handle_update used by the
long-poll bot — including admin filtering and an Approve that transitions state.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from config.settings import Settings
from gelio.approval import ApprovalService, encode_callback
from gelio.schemas import PostRecord, PostState
from gelio.store import Store
from gelio.sync import SupabaseSync
from gelio.webhook import create_app
from tests.conftest import BRAND, make_content_dict

SECRET = "s3cret-token"
ADMIN = "999"
HEADER = "X-Telegram-Bot-Api-Secret-Token"


class _SpyApproval:
    """Records handle_update calls without doing any work."""

    def __init__(self) -> None:
        self.updates: list[dict] = []

    def handle_update(self, update: dict) -> None:
        self.updates.append(update)


@pytest.fixture
def spy_client():
    spy = _SpyApproval()
    return spy, TestClient(create_app(spy, SECRET))


def _cb(action, post_id, chat_id=ADMIN):
    return {
        "update_id": 1,
        "callback_query": {
            "id": "cq1",
            "data": encode_callback(action, post_id),
            "message": {"message_id": 50, "chat": {"id": int(chat_id)}},
        },
    }


# --------------------------------------------------------------------------- #
# Secret validation
# --------------------------------------------------------------------------- #
def test_health(spy_client):
    _spy, client = spy_client
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["ok"] is True


def test_valid_secret_routes(spy_client):
    spy, client = spy_client
    r = client.post(f"/telegram/{SECRET}", json=_cb("approve", "p1"), headers={HEADER: SECRET})
    assert r.status_code == 200
    assert len(spy.updates) == 1


def test_bad_path_secret_rejected(spy_client):
    spy, client = spy_client
    r = client.post("/telegram/wrong", json=_cb("approve", "p1"), headers={HEADER: SECRET})
    assert r.status_code == 403
    assert spy.updates == []


def test_bad_header_secret_rejected(spy_client):
    spy, client = spy_client
    r = client.post(f"/telegram/{SECRET}", json=_cb("approve", "p1"), headers={HEADER: "nope"})
    assert r.status_code == 403
    assert spy.updates == []


def test_missing_header_rejected(spy_client):
    spy, client = spy_client
    r = client.post(f"/telegram/{SECRET}", json=_cb("approve", "p1"))
    assert r.status_code == 403
    assert spy.updates == []


def test_handler_error_still_200():
    class Boom:
        def handle_update(self, update):
            raise RuntimeError("kaboom")

    client = TestClient(create_app(Boom(), SECRET))
    r = client.post(f"/telegram/{SECRET}", json=_cb("approve", "p1"), headers={HEADER: SECRET})
    assert r.status_code == 200  # never 500 → Telegram won't retry-storm


# --------------------------------------------------------------------------- #
# End-to-end through a REAL ApprovalService (shared handlers)
# --------------------------------------------------------------------------- #
class _FakeTelegram:
    def __init__(self):
        self.edits = []
        self.answers = []

    def send_media_group(self, *a, **k):
        return [{"message_id": 1}]

    def send_message(self, *a, **k):
        return {"message_id": 1}

    def send_document(self, *a, **k):
        return {"message_id": 1}

    def edit_message_text(self, chat_id, message_id, text):
        self.edits.append(text)

    def answer_callback_query(self, cq_id, text=None):
        self.answers.append(text)

    def get_updates(self, offset, timeout):
        return []


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        provider="groq", groq_api_key="", groq_model="m", gemini_api_key="", gemini_model="m",
        brand=BRAND, telegram_bot_token="t", telegram_admin_chat_id=ADMIN,
        output_dir=tmp_path / "output", db_path=tmp_path / "db.sqlite",
    )


def _seed_awaiting(store, settings, post_id):
    out = settings.output_dir / post_id
    (out / "slides").mkdir(parents=True, exist_ok=True)
    for i in range(1, 4):
        (out / "slides" / f"slide_{i}.png").write_bytes(b"x")
    (out / "content.json").write_text(json.dumps(make_content_dict(post_id, 9, BRAND)), encoding="utf-8")
    (out / "brief.json").write_text(json.dumps({"concept": "C", "hook": "a hook here"}), encoding="utf-8")
    store.record_draft(PostRecord(id=post_id, concept="C", date=post_id[:10]))
    store.transition(post_id, PostState.AWAITING_APPROVAL)


def test_real_approve_transitions_state(tmp_path):
    settings = _settings(tmp_path)
    store = Store(settings.db_path)
    published: list[str] = []
    svc = ApprovalService(
        _FakeTelegram(), store, settings, SupabaseSync(settings),
        regen_runner=lambda *a: "x", publish_runner=published.append,
    )
    _seed_awaiting(store, settings, "2026-06-25-c")
    client = TestClient(create_app(svc, SECRET))

    r = client.post(
        f"/telegram/{SECRET}", json=_cb("approve", "2026-06-25-c"), headers={HEADER: SECRET}
    )
    assert r.status_code == 200
    assert store.get_post("2026-06-25-c").state is PostState.APPROVED
    assert published == ["2026-06-25-c"]  # unscheduled approve published inline
    store.close()


def test_real_non_admin_ignored(tmp_path):
    settings = _settings(tmp_path)
    store = Store(settings.db_path)
    svc = ApprovalService(
        _FakeTelegram(), store, settings, SupabaseSync(settings), regen_runner=lambda *a: "x"
    )
    _seed_awaiting(store, settings, "2026-06-25-c")
    client = TestClient(create_app(svc, SECRET))

    r = client.post(
        f"/telegram/{SECRET}",
        json=_cb("approve", "2026-06-25-c", chat_id="111"),  # not an admin
        headers={HEADER: SECRET},
    )
    assert r.status_code == 200
    assert store.get_post("2026-06-25-c").state is PostState.AWAITING_APPROVAL  # unchanged
    store.close()
