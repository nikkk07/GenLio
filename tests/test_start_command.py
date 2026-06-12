"""/start command: generates a post and broadcasts the preview to all admins.

Replaces the old root-level test_start_command.py script — this version is a
real offline unit test (the pipeline is stubbed, nothing touches Telegram).
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

import gelio.pipeline as pipeline_mod
from gelio.approval import ApprovalService
from gelio.schemas import PostState
from gelio.store import Store
from gelio.sync import SupabaseSync
from tests.test_approval import ADMIN, FakeTelegram, RegenStub, _seed, _settings

ADMIN2 = "888"
POST_ID = "2026-06-11-start-topic"


@dataclass
class _FakeBrief:
    id: str
    concept: str


class _FakeRunResult:
    def __init__(self, post_id: str) -> None:
        self.brief = _FakeBrief(id=post_id, concept="Start Topic")
        self.content = type("C", (), {"slides": [1] * 9})()
        self.already_existed = False


@pytest.fixture
def env(tmp_path, monkeypatch):
    settings = _settings(tmp_path, admin=f"{ADMIN},{ADMIN2}")
    store = Store(settings.db_path)
    tg = FakeTelegram()
    svc = ApprovalService(tg, store, settings, SupabaseSync(settings), RegenStub(store, settings))

    class _FakePipeline:
        def generate(self, **kwargs):
            # The post the pipeline "made": seed artifacts so send_preview works.
            _seed(store, settings, POST_ID, "Start Topic", "2026-06-11", PostState.DRAFTED)
            return _FakeRunResult(POST_ID)

    class _ClosableStore:
        def close(self) -> None:
            pass

    monkeypatch.setattr(
        pipeline_mod, "build_pipeline", lambda s: (_FakePipeline(), _ClosableStore())
    )
    yield settings, store, tg, svc
    store.close()


def _start_message(chat_id: str) -> dict:
    return {
        "update_id": 9,
        "message": {
            "message_id": 1,
            "chat": {"id": int(chat_id)},
            "text": "/start",
        },
    }


def test_start_generates_and_broadcasts_preview(env):
    settings, store, tg, svc = env
    svc.handle_update(_start_message(ADMIN))

    # Preview album + buttons reached every admin.
    assert len(tg.media_groups) == 2
    button_chats = [c for c, t in tg.message_chats if "Choose an action" in t]
    assert sorted(button_chats) == sorted([ADMIN, ADMIN2])
    assert store.get_post(POST_ID).state is PostState.AWAITING_APPROVAL


def test_start_from_second_admin_works(env):
    settings, store, tg, svc = env
    svc.handle_update(_start_message(ADMIN2))
    assert store.get_post(POST_ID) is not None


def test_start_from_non_admin_is_ignored(env):
    settings, store, tg, svc = env
    svc.handle_update(_start_message("31337"))
    assert store.get_post(POST_ID) is None
    assert tg.messages == []
