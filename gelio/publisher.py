"""Phase 4: the publishers — X, Instagram, and the semi-manual LinkedIn path.

A post in state ``APPROVED`` is cleared to publish. ``PublishService``
orchestrates per-platform publishers and the state machine:

* **X** — OAuth 1.0a user-context, hand-rolled HMAC-SHA1 over httpx. We sign
  requests ourselves rather than pulling in ``requests-oauthlib`` (which would
  drag the ``requests`` stack into an httpx codebase) — RFC 5849 signing is
  ~40 lines, fully deterministic, and verified by a golden test against
  Twitter's own documented example signature.
* **Instagram** — Graph API carousel publishing. Requires the Supabase public
  slide URLs that sync uploads (Graph API fetches ``image_url`` itself), so
  the publisher skips gracefully when Supabase is not configured.
* **LinkedIn** — semi-manual by design: the carousel PDF + ready-to-paste
  caption go to every admin on Telegram with a "Mark LinkedIn posted" button.

Idempotency: per-platform ``*_status`` columns are checked before every
attempt, so a retry (``publish <id>``) only touches failed/unfinished
platforms and never double-posts. Platform post ids are recorded for audit.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets as _secrets
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from config.settings import Settings
from gelio.redact import redact
from gelio.schemas import Content, PostRecord, PostState
from gelio.store import StateStore, StateTransitionError
from gelio.sync import SupabaseSync

logger = logging.getLogger("gelio.publisher")

_TIMEOUT = httpx.Timeout(60.0, connect=10.0)

PLATFORM_ORDER = ("x", "instagram", "linkedin")


class PublishError(RuntimeError):
    """Base class for publishing failures."""


class RetryablePublishError(PublishError):
    """Transient failure (429 / 5xx / network) — retried with backoff."""


class PermanentPublishError(PublishError):
    """Permanent 4xx failure — fail fast, no retry."""


_net_retry = retry(
    retry=retry_if_exception_type((httpx.TransportError, RetryablePublishError)),
    wait=wait_exponential(multiplier=1, min=2, max=20),
    stop=stop_after_attempt(3),
    reraise=True,
)


def _classify(resp: httpx.Response, platform: str) -> None:
    """Raise the right PublishError for a non-2xx response (body redacted)."""
    if resp.status_code < 400:
        return
    body = redact(resp.text[:500])
    msg = f"{platform} HTTP {resp.status_code}: {body}"
    if resp.status_code == 429 or resp.status_code >= 500:
        raise RetryablePublishError(msg)
    raise PermanentPublishError(msg)


# --------------------------------------------------------------------------- #
# OAuth 1.0a (RFC 5849) signing — pure and golden-testable
# --------------------------------------------------------------------------- #
def percent_encode(value: str) -> str:
    """RFC 3986 percent-encoding as OAuth 1.0a requires (no safe chars beyond
    unreserved)."""
    return urllib.parse.quote(str(value), safe="~-._")


def oauth1_signature(
    method: str,
    url: str,
    params: dict[str, str],
    consumer_secret: str,
    token_secret: str,
) -> str:
    """HMAC-SHA1 signature over the OAuth base string."""
    encoded = sorted(
        (percent_encode(k), percent_encode(v)) for k, v in params.items()
    )
    param_string = "&".join(f"{k}={v}" for k, v in encoded)
    base = "&".join(
        (method.upper(), percent_encode(url), percent_encode(param_string))
    )
    signing_key = f"{percent_encode(consumer_secret)}&{percent_encode(token_secret)}"
    digest = hmac.new(signing_key.encode(), base.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def oauth1_header(
    method: str,
    url: str,
    *,
    consumer_key: str,
    consumer_secret: str,
    token: str,
    token_secret: str,
    extra_params: dict[str, str] | None = None,
    nonce: str | None = None,
    timestamp: str | None = None,
) -> str:
    """Build the ``Authorization: OAuth …`` header for a request.

    ``extra_params`` are request parameters that participate in the signature
    (query/form-urlencoded params). JSON and multipart bodies are *not*
    signed, per spec — pass nothing for those.
    """
    oauth_params = {
        "oauth_consumer_key": consumer_key,
        "oauth_nonce": nonce or _secrets.token_hex(16),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": timestamp or str(int(time.time())),
        "oauth_token": token,
        "oauth_version": "1.0",
    }
    all_params = {**(extra_params or {}), **oauth_params}
    signature = oauth1_signature(method, url, all_params, consumer_secret, token_secret)
    header_params = {**oauth_params, "oauth_signature": signature}
    parts = ", ".join(
        f'{percent_encode(k)}="{percent_encode(v)}"'
        for k, v in sorted(header_params.items())
    )
    return f"OAuth {parts}"


# --------------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------------- #
@dataclass
class PlatformResult:
    """Outcome of one platform attempt within a publish run."""

    platform: str
    ok: bool
    skipped: bool = False
    detail: str = ""
    ref: str | None = None  # tweet id / IG media id


@dataclass
class PublishReport:
    """Outcome of a full publish run for one post."""

    post_id: str
    results: list[PlatformResult] = field(default_factory=list)
    final_state: PostState | None = None

    def summary(self) -> str:
        icons = {"x": "🐦 X", "instagram": "📸 Instagram", "linkedin": "💼 LinkedIn"}
        parts: list[str] = []
        for r in self.results:
            label = icons.get(r.platform, r.platform)
            if r.platform == "linkedin" and r.ok:
                parts.append(f"{label} PDF sent — upload manually")
            elif r.ok:
                parts.append(f"{label} ✅")
            elif r.skipped:
                parts.append(f"{label} ⏭ {r.detail}")
            else:
                parts.append(f"{label} ❌ {r.detail}")
        line = " · ".join(parts) if parts else "nothing to publish"
        if self.final_state is PostState.COMPLETE:
            line += "\n🏁 All enabled platforms done — post COMPLETE."
        return line


# --------------------------------------------------------------------------- #
# Platform publishers
# --------------------------------------------------------------------------- #
class Publisher(Protocol):
    """One platform's publish behavior."""

    name: str

    @property
    def enabled(self) -> bool: ...
    def disabled_reason(self) -> str: ...
    def publish(
        self, record: PostRecord, content: Content, slide_paths: list[Path], pdf_path: Path
    ) -> PlatformResult: ...


def compose_x_text(caption: str, hashtags: list[str], limit: int = 280) -> str:
    """Caption + as many hashtags as fit; never exceeds ``limit``."""
    text = caption.strip()
    if len(text) > limit:  # defensive — schema already caps at 280
        text = text[: limit - 1].rstrip() + "…"
    for tag in hashtags:
        candidate = f"{text} {tag}"
        if len(candidate) > limit:
            break
        text = candidate
    return text


class XPublisher:
    """X (Twitter): simple media upload per slide, then POST /2/tweets."""

    name = "x"
    MEDIA_UPLOAD_URL = "https://upload.twitter.com/1.1/media/upload.json"
    TWEET_URL = "https://api.twitter.com/2/tweets"

    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self.max_images = max(1, min(settings.x_max_images, 4))  # X hard cap: 4

    @property
    def enabled(self) -> bool:
        return all(
            (
                self._s.x_consumer_key,
                self._s.x_consumer_secret,
                self._s.x_access_token,
                self._s.x_access_token_secret,
            )
        )

    def disabled_reason(self) -> str:
        return "X keys not set (X_CONSUMER_KEY/SECRET, X_ACCESS_TOKEN/SECRET)"

    def _auth(self, method: str, url: str, extra_params: dict[str, str] | None = None) -> str:
        return oauth1_header(
            method,
            url,
            consumer_key=self._s.x_consumer_key,
            consumer_secret=self._s.x_consumer_secret,
            token=self._s.x_access_token,
            token_secret=self._s.x_access_token_secret,
            extra_params=extra_params,
        )

    @_net_retry
    def _upload_media(self, path: Path) -> str:
        # Multipart body params are not signed (RFC 5849 §3.4.1.3.1).
        headers = {"Authorization": self._auth("POST", self.MEDIA_UPLOAD_URL)}
        with httpx.Client(timeout=_TIMEOUT) as client:
            resp = client.post(
                self.MEDIA_UPLOAD_URL,
                headers=headers,
                files={"media": (path.name, path.read_bytes(), "image/png")},
            )
        _classify(resp, "x media upload")
        media_id = resp.json().get("media_id_string")
        if not media_id:
            raise PermanentPublishError(f"x media upload returned no id: {redact(resp.text[:200])}")
        return media_id

    @_net_retry
    def _create_tweet(self, text: str, media_ids: list[str]) -> str:
        headers = {"Authorization": self._auth("POST", self.TWEET_URL)}
        payload: dict[str, Any] = {"text": text}
        if media_ids:
            payload["media"] = {"media_ids": media_ids}
        with httpx.Client(timeout=_TIMEOUT) as client:
            resp = client.post(self.TWEET_URL, headers=headers, json=payload)
        _classify(resp, "x tweet")
        tweet_id = (resp.json().get("data") or {}).get("id")
        if not tweet_id:
            raise PermanentPublishError(f"x tweet returned no id: {redact(resp.text[:200])}")
        return tweet_id

    def publish(
        self, record: PostRecord, content: Content, slide_paths: list[Path], pdf_path: Path
    ) -> PlatformResult:
        # Carousels run 8–10 slides but X allows 4 images: post the first N
        # (hook + leading insights). Selection strategy: first X_MAX_IMAGES.
        selected = slide_paths[: self.max_images]
        media_ids = [self._upload_media(p) for p in selected]
        text = compose_x_text(content.captions.x, content.hashtags)
        tweet_id = self._create_tweet(text, media_ids)
        logger.info(
            "x posted id=%s tweet=%s images=%d", record.id, tweet_id, len(media_ids)
        )
        return PlatformResult("x", ok=True, ref=tweet_id, detail=f"{len(media_ids)} images")


class InstagramPublisher:
    """Instagram Graph API carousel: item containers → carousel → publish.

    ``image_url`` must be publicly reachable, so the publisher relies on the
    Supabase public bucket URLs that sync uploads. Without Supabase it skips.
    """

    name = "instagram"
    POLL_INTERVAL = 2.0
    POLL_MAX = 30

    def __init__(
        self,
        settings: Settings,
        sync: SupabaseSync,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._s = settings
        self._sync = sync
        self._sleep = sleep

    @property
    def enabled(self) -> bool:
        return bool(self._s.ig_user_id and self._s.ig_access_token)

    def disabled_reason(self) -> str:
        return "Instagram keys not set (IG_USER_ID, IG_ACCESS_TOKEN)"

    @_net_retry
    def _post(self, url: str, data: dict[str, Any]) -> dict[str, Any]:
        with httpx.Client(timeout=_TIMEOUT) as client:
            resp = client.post(url, data={**data, "access_token": self._s.ig_access_token})
        _classify(resp, "instagram")
        return resp.json()

    @_net_retry
    def _get(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        with httpx.Client(timeout=_TIMEOUT) as client:
            resp = client.get(url, params={**params, "access_token": self._s.ig_access_token})
        _classify(resp, "instagram")
        return resp.json()

    def _wait_finished(self, container_id: str) -> None:
        url = f"{self._s.ig_api_base}/{container_id}"
        for _ in range(self.POLL_MAX):
            body = self._get(url, {"fields": "status_code"})
            status = body.get("status_code")
            if status == "FINISHED":
                return
            if status == "ERROR":
                raise PermanentPublishError(
                    f"instagram container {container_id} errored: {redact(json.dumps(body)[:300])}"
                )
            self._sleep(self.POLL_INTERVAL)
        raise RetryablePublishError(f"instagram container {container_id} never finished")

    def publish(
        self, record: PostRecord, content: Content, slide_paths: list[Path], pdf_path: Path
    ) -> PlatformResult:
        if not self._sync.enabled:
            return PlatformResult(
                "instagram",
                ok=False,
                skipped=True,
                detail="Instagram requires Supabase slide URLs — enable sync",
            )
        # Re-upload (x-upsert) so the public URLs are guaranteed to exist even
        # if the render-time push failed.
        assets = self._sync.upload_assets(record.id, slide_paths, None)
        slide_urls: list[str] = assets["slide_urls"]
        if not slide_urls:
            raise PermanentPublishError("no public slide URLs available for instagram")

        media_url = f"{self._s.ig_api_base}/{self._s.ig_user_id}/media"
        children: list[str] = []
        for url in slide_urls:
            body = self._post(media_url, {"image_url": url, "is_carousel_item": "true"})
            children.append(str(body["id"]))

        caption = f"{content.captions.instagram}\n\n{' '.join(content.hashtags)}"
        carousel = self._post(
            media_url,
            {
                "media_type": "CAROUSEL",
                "children": ",".join(children),
                "caption": caption,
            },
        )
        carousel_id = str(carousel["id"])
        self._wait_finished(carousel_id)

        publish_url = f"{self._s.ig_api_base}/{self._s.ig_user_id}/media_publish"
        published = self._post(publish_url, {"creation_id": carousel_id})
        media_id = str(published["id"])
        logger.info(
            "instagram posted id=%s media=%s slides=%d", record.id, media_id, len(children)
        )
        return PlatformResult("instagram", ok=True, ref=media_id, detail=f"{len(children)} slides")


class LinkedInPublisher:
    """LinkedIn is semi-manual: deliver the PDF + caption to all admins with a
    Mark-posted button; the button tap (handled by the bot) finishes the job."""

    name = "linkedin"

    def __init__(self, telegram: Any, admin_chat_ids: list[str]) -> None:
        self._tg = telegram
        self._admins = admin_chat_ids

    @property
    def enabled(self) -> bool:
        return self._tg is not None and bool(self._admins)

    def disabled_reason(self) -> str:
        return "Telegram not configured (TELEGRAM_BOT_TOKEN, TELEGRAM_ADMIN_CHAT_ID)"

    def publish(
        self, record: PostRecord, content: Content, slide_paths: list[Path], pdf_path: Path
    ) -> PlatformResult:
        if not pdf_path.exists():
            raise PermanentPublishError(f"carousel.pdf missing for {record.id}")
        caption = f"{content.captions.linkedin}\n\n{' '.join(content.hashtags)}"
        keyboard = {
            "inline_keyboard": [
                [{"text": "✅ Mark LinkedIn posted", "callback_data": f"l|{record.id}"}]
            ]
        }
        delivered = 0
        for admin in self._admins:
            try:
                self._tg.send_document(
                    admin,
                    pdf_path,
                    caption=f"💼 LinkedIn carousel for {record.id} — upload manually.",
                )
                self._tg.send_message(
                    admin,
                    f"Ready-to-paste LinkedIn caption:\n\n{caption}",
                    reply_markup=keyboard,
                )
                delivered += 1
            except Exception as exc:  # noqa: BLE001 - one admin failing ≠ all fail
                logger.warning("linkedin delivery to admin %s failed: %s", admin, redact(str(exc)))
        if delivered == 0:
            raise RetryablePublishError("linkedin PDF could not be delivered to any admin")
        logger.info("linkedin pdf sent id=%s admins=%d", record.id, delivered)
        return PlatformResult("linkedin", ok=True, detail=f"sent to {delivered} admin(s)")


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
# States from which publish() may act (everything post-approval, pre-complete).
_PUBLISHABLE = {
    PostState.APPROVED,
    PostState.POSTED_X,
    PostState.POSTED_IG,
    PostState.LINKEDIN_PENDING,
    PostState.FAILED_POST,
    PostState.FAILED_X,
    PostState.FAILED_IG,
}

# What "done" means per platform, read off the record's status columns.
_DONE_STATUS = {"x": {"posted"}, "instagram": {"posted"}, "linkedin": {"posted"}}
_IN_FLIGHT_STATUS = {"linkedin": {"sent"}}


def _platform_status(record: PostRecord, platform: str) -> str | None:
    return {
        "x": record.x_status,
        "instagram": record.ig_status,
        "linkedin": record.linkedin_status,
    }[platform]


class PublishService:
    """Drives per-platform publishers and the post state machine."""

    def __init__(
        self,
        store: StateStore,
        settings: Settings,
        sync: SupabaseSync,
        publishers: dict[str, Publisher],
        notify: Callable[[str], None] | None = None,
    ) -> None:
        self._store = store
        self._settings = settings
        self._sync = sync
        self._publishers = publishers
        # notify(text) broadcasts a result message to all admins (or no-ops
        # when Telegram isn't configured, e.g. key-less CLI runs).
        self._notify = notify or (lambda text: None)

    # -- helpers ---------------------------------------------------------------
    def _load_content(self, post_id: str) -> Content:
        path = self._settings.output_dir / post_id / "content.json"
        return Content.model_validate(json.loads(path.read_text(encoding="utf-8")))

    def _slide_paths(self, post_id: str) -> list[Path]:
        slides_dir = self._settings.output_dir / post_id / "slides"
        if not slides_dir.exists():
            return []
        return sorted(
            slides_dir.glob("slide_*.png"), key=lambda p: int(p.stem.split("_")[1])
        )

    def _safe_transition(self, post_id: str, new_state: PostState) -> PostRecord:
        record = self._store.get_post(post_id)
        if record is not None and record.state == new_state:
            return record
        try:
            return self._store.transition(post_id, new_state)
        except StateTransitionError as exc:
            logger.warning("transition skipped id=%s -> %s: %s", post_id, new_state.value, exc)
            return self._store.get_post(post_id)  # type: ignore[return-value]

    def _refresh_state(self, post_id: str) -> PostRecord:
        """Derive the milestone state from per-platform statuses and apply it.

        Disabled platforms are excluded from completion: when every *enabled*
        platform has succeeded, the post is COMPLETE.
        """
        record = self._store.get_post(post_id)
        assert record is not None
        enabled = [name for name, p in self._publishers.items() if p.enabled]
        statuses = {name: _platform_status(record, name) for name in enabled}

        if enabled and all(s == "posted" for s in statuses.values()):
            target = PostState.COMPLETE
        else:
            failed = [n for n, s in statuses.items() if s == "failed"]
            if failed == ["x"]:
                target = PostState.FAILED_X
            elif failed == ["instagram"]:
                target = PostState.FAILED_IG
            elif failed:
                target = PostState.FAILED_POST
            elif statuses.get("linkedin") == "sent":
                target = PostState.LINKEDIN_PENDING
            elif statuses.get("instagram") == "posted":
                target = PostState.POSTED_IG
            elif statuses.get("x") == "posted":
                target = PostState.POSTED_X
            else:
                return record  # nothing happened yet
        updated = self._safe_transition(post_id, target)
        self._sync.push_state(updated)
        return updated

    # -- main entry points -------------------------------------------------------
    def publish(self, post_id: str, platforms: list[str] | None = None) -> PublishReport:
        """Publish one approved post now (idempotent per platform)."""
        record = self._store.get_post(post_id)
        if record is None:
            raise PublishError(f"unknown post id {post_id!r}")
        report = PublishReport(post_id=post_id)
        if record.state is PostState.COMPLETE:
            report.final_state = record.state
            logger.info("publish no-op id=%s already COMPLETE", post_id)
            return report
        if record.state not in _PUBLISHABLE:
            raise PublishError(
                f"post {post_id!r} is {record.state.value}; approve it before publishing"
            )

        content = self._load_content(post_id)
        slide_paths = self._slide_paths(post_id)
        if not slide_paths:
            raise PublishError(f"no rendered slides for {post_id!r}; render it first")
        pdf_path = self._settings.output_dir / post_id / "carousel.pdf"

        wanted = platforms or list(PLATFORM_ORDER)
        for name in PLATFORM_ORDER:
            if name not in wanted or name not in self._publishers:
                continue
            publisher = self._publishers[name]
            record = self._store.get_post(post_id)  # re-read: statuses evolve
            assert record is not None
            status = _platform_status(record, name)
            if status in _DONE_STATUS[name]:
                report.results.append(
                    PlatformResult(name, ok=True, skipped=True, detail="already posted", ref=None)
                )
                continue
            if status in _IN_FLIGHT_STATUS.get(name, set()):
                report.results.append(
                    PlatformResult(name, ok=True, skipped=True, detail="awaiting manual confirm")
                )
                continue
            if not publisher.enabled:
                logger.info("publisher %s disabled: %s", name, publisher.disabled_reason())
                report.results.append(
                    PlatformResult(name, ok=False, skipped=True, detail=publisher.disabled_reason())
                )
                continue
            try:
                result = publisher.publish(record, content, slide_paths, pdf_path)
            except PublishError as exc:
                logger.error("publish %s failed id=%s: %s", name, post_id, redact(str(exc)))
                result = PlatformResult(name, ok=False, detail=redact(str(exc))[:200])
            except httpx.TransportError as exc:
                logger.error("publish %s network failure id=%s: %s", name, post_id, redact(str(exc)))
                result = PlatformResult(name, ok=False, detail=f"network: {redact(str(exc))[:150]}")
            report.results.append(result)
            if result.skipped:
                continue  # e.g. IG without Supabase: no status written, retryable any time
            if result.ok:
                status_value = "sent" if name == "linkedin" else "posted"
                self._store.set_platform_result(post_id, _column_key(name), status_value, result.ref)
            else:
                self._store.set_platform_result(post_id, _column_key(name), "failed")
            # Milestone after every platform (POSTED_X, LINKEDIN_PENDING, …) so
            # the dashboard tracks progress even if a later platform hangs.
            self._refresh_state(post_id)

        final = self._refresh_state(post_id)
        report.final_state = final.state
        summary = report.summary()
        logger.info("publish report id=%s state=%s: %s", post_id, final.state.value, summary)
        self._notify(f"📤 Publish results for {post_id}:\n{summary}")
        return report

    def publish_due(self, now_iso: str | None = None) -> list[PublishReport]:
        """Publish every APPROVED post whose schedule is due (or unset).

        Safe to run repeatedly: published posts leave APPROVED, failed ones
        move to FAILED_* (retried explicitly via ``publish <id>``).
        """
        due = self._store.get_posts_due_for_posting(now_iso, include_unscheduled=True)
        if not due:
            logger.info("publish-due: nothing due")
            return []
        reports = []
        for record in due:
            logger.info(
                "publish-due: publishing id=%s scheduled=%s", record.id, record.scheduled_time
            )
            reports.append(self.publish(record.id))
        return reports

    def mark_linkedin_posted(self, post_id: str) -> PostRecord:
        """Advance LinkedIn from 'sent' to 'posted' (Mark-posted button)."""
        self._store.set_platform_result(post_id, "linkedin", "posted")
        return self._refresh_state(post_id)


def _column_key(platform: str) -> str:
    """Map publisher names to Store.set_platform_result keys."""
    return {"x": "x", "instagram": "ig", "linkedin": "linkedin"}[platform]


def build_publish_service(
    settings: Settings,
    store: StateStore,
    sync: SupabaseSync,
    telegram: Any | None = None,
) -> PublishService:
    """Wire the default publishers. ``telegram`` is the (shared) TelegramClient
    used for LinkedIn delivery and admin notifications; pass None for key-less
    CLI runs — LinkedIn is then reported as disabled."""
    admins = settings.telegram_admin_chat_ids
    publishers: dict[str, Publisher] = {
        "x": XPublisher(settings),
        "instagram": InstagramPublisher(settings, sync),
        "linkedin": LinkedInPublisher(telegram, admins),
    }

    def notify(text: str) -> None:
        if telegram is None:
            return
        for admin in admins:
            try:
                telegram.send_message(admin, text)
            except Exception as exc:  # noqa: BLE001
                logger.warning("notify admin %s failed: %s", admin, redact(str(exc)))

    return PublishService(store, settings, sync, publishers, notify=notify)
