"""Phase 4 publisher tests. No network: httpx is routed to a MockTransport
and Telegram/Supabase are fakes."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from config.settings import Settings
from gelio.publisher import (
    InstagramPublisher,
    LinkedInPublisher,
    PublishService,
    XPublisher,
    build_publish_service,
    compose_x_text,
    oauth1_header,
    oauth1_signature,
    percent_encode,
)
from gelio.schemas import PostRecord, PostState
from gelio.store import Store
from tests.conftest import BRAND, make_content_dict

ADMINS = ["999", "888"]


# --------------------------------------------------------------------------- #
# Fakes & fixtures
# --------------------------------------------------------------------------- #
class FakeTelegram:
    def __init__(self) -> None:
        self.documents: list[tuple[str, Path, str | None]] = []
        self.messages: list[tuple[str, str, dict | None]] = []  # (chat, text, markup)

    def send_document(self, chat_id, document_path, caption=None):
        self.documents.append((str(chat_id), document_path, caption))
        return {"message_id": 1}

    def send_message(self, chat_id, text, reply_markup=None):
        self.messages.append((str(chat_id), text, reply_markup))
        return {"message_id": 2}


class FakeSync:
    """Supabase stand-in: configurable enabled flag, canned public URLs."""

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self.pushed: list[PostRecord] = []
        self.uploads: list[str] = []

    def upload_assets(self, post_id, slide_paths, pdf_path):
        self.uploads.append(post_id)
        return {
            "slide_urls": [f"https://cdn.example/{post_id}/{p.name}" for p in slide_paths],
            "pdf_url": None,
        }

    def push_state(self, record, content=None):
        self.pushed.append(record)


def _settings(tmp_path: Path, **overrides) -> Settings:
    kwargs = dict(
        provider="groq",
        groq_api_key="",
        groq_model="m",
        gemini_api_key="",
        gemini_model="m",
        brand=BRAND,
        telegram_bot_token="t",
        telegram_admin_chat_id=",".join(ADMINS),
        output_dir=tmp_path / "output",
        db_path=tmp_path / "db.sqlite",
    )
    kwargs.update(overrides)
    return Settings(**kwargs)


X_KEYS = dict(
    x_consumer_key="ck",
    x_consumer_secret="cs",
    x_access_token="at",
    x_access_token_secret="ats",
)
IG_KEYS = dict(ig_user_id="17841400000000000", ig_access_token="igtok")


def _seed(store: Store, settings: Settings, post_id: str, *, n_slides: int = 9,
          state: PostState = PostState.APPROVED, scheduled: str | None = None) -> None:
    out = settings.output_dir / post_id
    (out / "slides").mkdir(parents=True, exist_ok=True)
    for i in range(1, n_slides + 1):
        (out / "slides" / f"slide_{i}.png").write_bytes(b"png")
    (out / "carousel.pdf").write_bytes(b"%PDF")
    (out / "content.json").write_text(
        json.dumps(make_content_dict(post_id, 9, BRAND)), encoding="utf-8"
    )
    store.record_draft(PostRecord(id=post_id, concept=post_id, date="2026-06-12"))
    store.transition(post_id, PostState.AWAITING_APPROVAL)
    if state != PostState.AWAITING_APPROVAL:
        store.transition(post_id, state)
    if scheduled:
        store.set_scheduled_time(post_id, scheduled)


@pytest.fixture
def no_retry_sleep(monkeypatch):
    """Make tenacity retries instant."""
    for fn in (
        XPublisher._upload_media,
        XPublisher._create_tweet,
        InstagramPublisher._post,
        InstagramPublisher._get,
    ):
        monkeypatch.setattr(fn.retry, "sleep", lambda s: None)


@pytest.fixture
def http(monkeypatch):
    """Route every httpx.Client in gelio.publisher through a scriptable handler."""
    state = {"handler": None, "requests": []}

    def handler(request: httpx.Request) -> httpx.Response:
        state["requests"].append(request)
        return state["handler"](request)

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    def client_factory(**kwargs):
        kwargs.pop("transport", None)
        return real_client(transport=transport, **kwargs)

    import gelio.publisher as pub_mod

    monkeypatch.setattr(pub_mod.httpx, "Client", client_factory)
    return state


def _service(settings, store, *, sync=None, telegram=None) -> PublishService:
    return build_publish_service(settings, store, sync or FakeSync(enabled=False), telegram=telegram)


# --------------------------------------------------------------------------- #
# OAuth 1.0a signing (golden test from Twitter's "Creating a signature" doc)
# --------------------------------------------------------------------------- #
def test_oauth1_signature_golden():
    params = {
        "status": "Hello Ladies + Gentlemen, a signed OAuth request!",
        "include_entities": "true",
        "oauth_consumer_key": "xvz1evFS4wEEPTGEFPHBog",
        "oauth_nonce": "kYjzVBB8Y0ZFabxSWbWovY3uYSQ2pTgmZeNu2VS4cg",
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": "1318622958",
        "oauth_token": "370773112-GmHxMAgYyLbNEtIKZeRNFsMKPR9EyMZeS9weJAEb",
        "oauth_version": "1.0",
    }
    sig = oauth1_signature(
        "POST",
        "https://api.twitter.com/1.1/statuses/update.json",
        params,
        "kAcSOqF21Fu85e7zjz7ZN2U4ZRhfV3WpwPAoE3Z7kBw",
        "LswwdoUaIvS8ltyTt5jkRh4J50vUPVVHtR2YPi5kE",
    )
    assert sig == "hCtSmYh+iHYCEqBWrE7C7hYmtUk="


def test_oauth1_header_is_wellformed():
    header = oauth1_header(
        "POST",
        "https://api.twitter.com/2/tweets",
        consumer_key="ck",
        consumer_secret="cs",
        token="at",
        token_secret="ats",
        nonce="fixednonce",
        timestamp="1700000000",
    )
    assert header.startswith("OAuth ")
    for key in ("oauth_consumer_key", "oauth_nonce", "oauth_signature",
                "oauth_signature_method", "oauth_timestamp", "oauth_token", "oauth_version"):
        assert f'{key}="' in header
    assert 'oauth_signature_method="HMAC-SHA1"' in header


def test_percent_encode_rfc3986():
    assert percent_encode("Hello Ladies + Gentlemen") == "Hello%20Ladies%20%2B%20Gentlemen"
    assert percent_encode("~safe-._") == "~safe-._"


# --------------------------------------------------------------------------- #
# X composition rules
# --------------------------------------------------------------------------- #
def test_compose_x_text_appends_hashtags_that_fit():
    text = compose_x_text("Short caption.", ["#one", "#two"])
    assert text == "Short caption. #one #two"


def test_compose_x_text_never_exceeds_280():
    caption = "c" * 275
    text = compose_x_text(caption, ["#aviation", "#x"])
    assert len(text) <= 280
    assert text.startswith(caption)  # caption intact, oversized tags dropped
    assert "#aviation" not in text


def test_compose_x_text_trims_overlong_caption():
    text = compose_x_text("c" * 300, ["#a"])
    assert len(text) <= 280
    assert text.endswith("…") or text.endswith("#a")


# --------------------------------------------------------------------------- #
# X publisher
# --------------------------------------------------------------------------- #
def _x_handler_ok(request: httpx.Request) -> httpx.Response:
    if "upload.twitter.com" in str(request.url):
        return httpx.Response(200, json={"media_id_string": f"m{len(str(request))%97}"})
    return httpx.Response(201, json={"data": {"id": "tweet123"}})


def test_x_publish_caps_at_four_images(tmp_path, http, no_retry_sleep):
    settings = _settings(tmp_path, **X_KEYS)
    store = Store(settings.db_path)
    _seed(store, settings, "p1", n_slides=9)
    http["handler"] = _x_handler_ok

    # LinkedIn enabled (FakeTelegram) so completion isn't reached and the
    # POSTED_X milestone is observable after an x-only publish.
    service = _service(settings, store, telegram=FakeTelegram())
    report = service.publish("p1", platforms=["x"])

    uploads = [r for r in http["requests"] if "upload.twitter.com" in str(r.url)]
    assert len(uploads) == 4  # 9 slides, X cap is 4
    x_result = next(r for r in report.results if r.platform == "x")
    assert x_result.ok and x_result.ref == "tweet123"
    record = store.get_post("p1")
    assert record.x_status == "posted" and record.x_post_id == "tweet123"
    assert record.state is PostState.POSTED_X
    store.close()


def test_x_publish_sends_oauth_header(tmp_path, http, no_retry_sleep):
    settings = _settings(tmp_path, **X_KEYS)
    store = Store(settings.db_path)
    _seed(store, settings, "p1")
    http["handler"] = _x_handler_ok
    _service(settings, store).publish("p1", platforms=["x"])
    for req in http["requests"]:
        assert req.headers["Authorization"].startswith("OAuth ")
    store.close()


def test_x_500_retries_then_succeeds(tmp_path, http, no_retry_sleep):
    settings = _settings(tmp_path, x_max_images=1, **X_KEYS)
    store = Store(settings.db_path)
    _seed(store, settings, "p1")
    calls = {"n": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        if "upload.twitter.com" in str(request.url):
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(500, text="server sad")
            return httpx.Response(200, json={"media_id_string": "m1"})
        return httpx.Response(201, json={"data": {"id": "t1"}})

    http["handler"] = flaky
    report = _service(settings, store).publish("p1", platforms=["x"])
    assert calls["n"] == 2
    assert next(r for r in report.results if r.platform == "x").ok
    store.close()


def test_x_permanent_4xx_fails_fast_and_marks_failed(tmp_path, http, no_retry_sleep):
    settings = _settings(tmp_path, x_max_images=1, **X_KEYS)
    store = Store(settings.db_path)
    _seed(store, settings, "p1")
    calls = {"n": 0}

    def forbidden(request):
        calls["n"] += 1
        return httpx.Response(403, json={"errors": [{"message": "nope"}]})

    http["handler"] = forbidden
    report = _service(settings, store).publish("p1", platforms=["x"])
    assert calls["n"] == 1  # no retries on permanent 4xx
    record = store.get_post("p1")
    assert record.x_status == "failed"
    assert record.state is PostState.FAILED_X
    assert not next(r for r in report.results if r.platform == "x").ok
    store.close()


def test_x_double_publish_is_noop(tmp_path, http, no_retry_sleep):
    settings = _settings(tmp_path, **X_KEYS)
    store = Store(settings.db_path)
    _seed(store, settings, "p1")
    http["handler"] = _x_handler_ok
    # LinkedIn enabled so the post isn't COMPLETE after X alone — the second
    # publish must hit the per-platform "already posted" check, not the
    # COMPLETE no-op.
    service = _service(settings, store, telegram=FakeTelegram())
    service.publish("p1", platforms=["x"])
    first_count = len(http["requests"])

    report2 = service.publish("p1", platforms=["x"])
    assert len(http["requests"]) == first_count  # no new network calls
    x2 = next(r for r in report2.results if r.platform == "x")
    assert x2.skipped and x2.detail == "already posted"
    store.close()


def test_x_disabled_without_keys(tmp_path, http):
    settings = _settings(tmp_path)  # no X keys
    store = Store(settings.db_path)
    _seed(store, settings, "p1")
    report = _service(settings, store).publish("p1", platforms=["x"])
    x = next(r for r in report.results if r.platform == "x")
    assert x.skipped and "X keys not set" in x.detail
    assert http["requests"] == []  # never touched the network
    assert store.get_post("p1").state is PostState.APPROVED
    store.close()


# --------------------------------------------------------------------------- #
# Instagram publisher
# --------------------------------------------------------------------------- #
def _ig_handler(sequence: list[str]):
    """Asserting handler for child → carousel → poll → publish."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.method == "POST" and url.split("?")[0].endswith("/media"):
            body = request.read().decode()
            if "is_carousel_item" in body:
                sequence.append("child")
                return httpx.Response(200, json={"id": f"child{len(sequence)}"})
            assert "media_type=CAROUSEL" in body
            sequence.append("carousel")
            return httpx.Response(200, json={"id": "car1"})
        if request.method == "GET" and "status_code" in url:
            sequence.append("poll")
            status = "IN_PROGRESS" if sequence.count("poll") < 2 else "FINISHED"
            return httpx.Response(200, json={"status_code": status, "id": "car1"})
        if url.split("?")[0].endswith("/media_publish"):
            sequence.append("publish")
            return httpx.Response(200, json={"id": "igmedia9"})
        raise AssertionError(f"unexpected request {request.method} {url}")

    return handler


def test_instagram_full_carousel_flow(tmp_path, http, no_retry_sleep):
    settings = _settings(tmp_path, **IG_KEYS)
    store = Store(settings.db_path)
    _seed(store, settings, "p1", n_slides=3)
    sync = FakeSync(enabled=True)
    sequence: list[str] = []
    http["handler"] = _ig_handler(sequence)

    service = build_publish_service(settings, store, sync, telegram=None)
    # No instant-finish: stub the poll sleep.
    service._publishers["instagram"]._sleep = lambda s: None
    report = service.publish("p1", platforms=["instagram"])

    assert sequence[:3] == ["child", "child", "child"]
    assert sequence[3] == "carousel"
    assert "poll" in sequence and sequence[-1] == "publish"
    ig = next(r for r in report.results if r.platform == "instagram")
    assert ig.ok and ig.ref == "igmedia9"
    record = store.get_post("p1")
    assert record.ig_status == "posted" and record.ig_media_id == "igmedia9"
    assert sync.uploads == ["p1"]  # public URLs guaranteed via re-upload
    store.close()


def test_instagram_skips_gracefully_without_supabase(tmp_path, http):
    settings = _settings(tmp_path, **IG_KEYS)
    store = Store(settings.db_path)
    _seed(store, settings, "p1")
    service = build_publish_service(settings, store, FakeSync(enabled=False), telegram=None)
    report = service.publish("p1", platforms=["instagram"])

    ig = next(r for r in report.results if r.platform == "instagram")
    assert ig.skipped and "requires Supabase" in ig.detail
    record = store.get_post("p1")
    assert record.ig_status is None  # retryable any time, nothing recorded
    assert record.state is PostState.APPROVED
    assert http["requests"] == []
    store.close()


def test_instagram_container_error_surfaces_body(tmp_path, http, no_retry_sleep):
    settings = _settings(tmp_path, **IG_KEYS)
    store = Store(settings.db_path)
    _seed(store, settings, "p1", n_slides=1)

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.method == "POST" and url.split("?")[0].endswith("/media"):
            return httpx.Response(200, json={"id": "c1"})
        if request.method == "GET":
            return httpx.Response(200, json={"status_code": "ERROR", "id": "c1"})
        raise AssertionError(url)

    http["handler"] = handler
    service = build_publish_service(settings, store, FakeSync(enabled=True), telegram=None)
    service._publishers["instagram"]._sleep = lambda s: None
    report = service.publish("p1", platforms=["instagram"])
    ig = next(r for r in report.results if r.platform == "instagram")
    assert not ig.ok and "ERROR" in ig.detail
    assert store.get_post("p1").ig_status == "failed"
    assert store.get_post("p1").state is PostState.FAILED_IG
    store.close()


# --------------------------------------------------------------------------- #
# LinkedIn publisher
# --------------------------------------------------------------------------- #
def test_linkedin_sends_pdf_caption_and_button(tmp_path):
    settings = _settings(tmp_path)
    store = Store(settings.db_path)
    _seed(store, settings, "p1")
    tg = FakeTelegram()
    service = build_publish_service(settings, store, FakeSync(enabled=False), telegram=tg)
    report = service.publish("p1", platforms=["linkedin"])

    assert [d[0] for d in tg.documents] == ADMINS  # PDF to every admin
    assert all(d[1].name == "carousel.pdf" for d in tg.documents)
    # The caption text + Mark-posted button went out too.
    button_msgs = [m for m in tg.messages if m[2] and "inline_keyboard" in m[2]]
    assert len(button_msgs) == len(ADMINS)
    assert all("LinkedIn caption" in m[1] for m in button_msgs)
    assert button_msgs[0][2]["inline_keyboard"][0][0]["callback_data"] == "l|p1"

    li = next(r for r in report.results if r.platform == "linkedin")
    assert li.ok
    record = store.get_post("p1")
    assert record.linkedin_status == "sent"
    assert record.state is PostState.LINKEDIN_PENDING
    store.close()


def test_mark_linkedin_posted_completes(tmp_path):
    settings = _settings(tmp_path)
    store = Store(settings.db_path)
    _seed(store, settings, "p1")
    tg = FakeTelegram()
    service = build_publish_service(settings, store, FakeSync(enabled=False), telegram=tg)
    service.publish("p1", platforms=["linkedin"])

    updated = service.mark_linkedin_posted("p1")
    assert updated.linkedin_status == "posted"
    # LinkedIn is the only enabled platform (no X/IG keys) → COMPLETE.
    assert updated.state is PostState.COMPLETE
    store.close()


# --------------------------------------------------------------------------- #
# Completion semantics & resumability
# --------------------------------------------------------------------------- #
def test_all_enabled_platforms_done_is_complete(tmp_path, http, no_retry_sleep):
    settings = _settings(tmp_path, **X_KEYS, **IG_KEYS)
    store = Store(settings.db_path)
    _seed(store, settings, "p1", n_slides=3)
    tg = FakeTelegram()
    sync = FakeSync(enabled=True)
    sequence: list[str] = []
    ig_handler = _ig_handler(sequence)

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "twitter.com" in url:
            return _x_handler_ok(request)
        return ig_handler(request)

    http["handler"] = handler
    service = build_publish_service(settings, store, sync, telegram=tg)
    service._publishers["instagram"]._sleep = lambda s: None
    service.publish("p1")

    record = store.get_post("p1")
    assert record.x_status == "posted" and record.ig_status == "posted"
    assert record.linkedin_status == "sent"
    assert record.state is PostState.LINKEDIN_PENDING  # waiting on the manual confirm

    updated = service.mark_linkedin_posted("p1")
    assert updated.state is PostState.COMPLETE
    store.close()


def test_partial_failure_is_resumable(tmp_path, http, no_retry_sleep):
    """X succeeds, IG fails → FAILED_IG; a retry touches only IG, then COMPLETE."""
    settings = _settings(tmp_path, x_max_images=1, **X_KEYS, **IG_KEYS)
    store = Store(settings.db_path)
    _seed(store, settings, "p1", n_slides=1)
    sync = FakeSync(enabled=True)
    mode = {"ig_ok": False}
    sequence: list[str] = []
    ig_ok_handler = _ig_handler(sequence)

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "twitter.com" in url:
            return _x_handler_ok(request)
        if not mode["ig_ok"]:
            return httpx.Response(400, json={"error": {"message": "bad token"}})
        return ig_ok_handler(request)

    http["handler"] = handler
    service = build_publish_service(settings, store, sync, telegram=None)
    service._publishers["instagram"]._sleep = lambda s: None
    service.publish("p1")

    record = store.get_post("p1")
    assert record.x_status == "posted" and record.ig_status == "failed"
    assert record.state is PostState.FAILED_IG

    # Fix IG and retry the whole post: X must NOT be re-posted.
    mode["ig_ok"] = True
    tweet_calls_before = len([r for r in http["requests"] if "twitter.com" in str(r.url)])
    report = service.publish("p1")
    tweet_calls_after = len([r for r in http["requests"] if "twitter.com" in str(r.url)])

    assert tweet_calls_after == tweet_calls_before  # idempotent: no double tweet
    x = next(r for r in report.results if r.platform == "x")
    assert x.skipped and x.detail == "already posted"
    record = store.get_post("p1")
    assert record.ig_status == "posted"
    assert record.state is PostState.COMPLETE  # linkedin disabled (telegram=None) → excluded
    store.close()


def test_publish_complete_post_is_noop(tmp_path):
    settings = _settings(tmp_path)
    store = Store(settings.db_path)
    _seed(store, settings, "p1")
    store.transition("p1", PostState.COMPLETE)
    report = _service(settings, store).publish("p1")
    assert report.results == [] and report.final_state is PostState.COMPLETE
    store.close()


# --------------------------------------------------------------------------- #
# Scheduling: publish-due selection
# --------------------------------------------------------------------------- #
def test_publish_due_picks_due_and_unscheduled_only(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    store = Store(settings.db_path)
    _seed(store, settings, "due-post", scheduled="2026-06-12T05:00:00+00:00")
    _seed(store, settings, "future-post", scheduled="2099-01-01T00:00:00+00:00")
    _seed(store, settings, "unscheduled-post")
    _seed(store, settings, "not-approved", state=PostState.AWAITING_APPROVAL)

    service = _service(settings, store)
    published: list[str] = []
    monkeypatch.setattr(
        service, "publish", lambda pid, platforms=None: published.append(pid)
    )
    service.publish_due("2026-06-12T06:00:00+00:00")

    assert sorted(published) == ["due-post", "unscheduled-post"]
    store.close()


def test_publish_due_idempotent_when_nothing_due(tmp_path):
    settings = _settings(tmp_path)
    store = Store(settings.db_path)
    _seed(store, settings, "future-post", scheduled="2099-01-01T00:00:00+00:00")
    service = _service(settings, store)
    assert service.publish_due() == []
    assert service.publish_due() == []  # safe to run repeatedly
    store.close()


def test_scheduled_ist_input_converts_to_utc_and_is_due_correctly(tmp_path):
    from gelio.timeutil import parse_schedule_input

    settings = _settings(tmp_path)
    store = Store(settings.db_path)
    _seed(store, settings, "p1")
    # 18:00 IST == 12:30 UTC.
    store.set_scheduled_time("p1", parse_schedule_input("2026-06-13 18:00"))

    assert store.get_posts_due_for_posting("2026-06-13T12:29:00+00:00") == []
    due = store.get_posts_due_for_posting("2026-06-13T12:31:00+00:00")
    assert [p.id for p in due] == ["p1"]
    store.close()
