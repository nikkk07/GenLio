"""Phase 3: the Telegram approval gate.

After a post is rendered, gelio sends the admin a preview album + captions +
inline buttons. The admin's tap (or typed regenerate topic) drives the SQLite
state machine. Nothing publishes without an explicit Approve.

Library choice: plain ``httpx`` against the Telegram Bot API (sync), not
python-telegram-bot. The codebase is already synchronous ``httpx`` + ``tenacity``
everywhere; long-polling ``getUpdates`` and ``sendMediaGroup`` are a few POSTs,
and a fake client makes the flow trivial to unit-test without a network or an
asyncio runtime.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Protocol

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from config.settings import Settings
from gelio.schemas import Content, PostState
from gelio.store import StateStore, StateTransitionError
from gelio.sync import SupabaseSync

logger = logging.getLogger("gelio.approval")

# Telegram hard limits.
CAPTION_LIMIT = 1024
TEXT_LIMIT = 4096
MEDIA_GROUP_MAX = 10

REGEN_PROMPT = (
    "🔄 Send the topic name for the new version "
    "(or send `auto` to let gelio pick)."
)
_REGEN_MARKER = "[regen:{id}]"
_SCHEDULE_MARKER = "[schedule:{id}]"

# Callback actions. Encoded as "<code>|<post_id>" — Telegram caps callback_data
# at 64 bytes, so we use 1-char codes (and ids are length-bounded, see
# topic_engine) to stay well under the limit even for long typed topics.
ACTIONS = ("approve", "reject", "regen", "approve_schedule", "linkedin_done")
_ACTION_CODE = {
    "approve": "a",
    "reject": "r",
    "regen": "g",
    "approve_schedule": "s",
    "linkedin_done": "l",
}
_CODE_ACTION = {v: k for k, v in _ACTION_CODE.items()}


class ApprovalError(RuntimeError):
    """Raised on an unrecoverable Telegram API error."""


# --------------------------------------------------------------------------- #
# Pure helpers (easy to unit test)
# --------------------------------------------------------------------------- #
def chunk_list(items: list[Any], size: int = MEDIA_GROUP_MAX) -> list[list[Any]]:
    """Split ``items`` into chunks of at most ``size`` (Telegram album limit)."""
    return [items[i : i + size] for i in range(0, len(items), size)]


def split_text(text: str, limit: int = TEXT_LIMIT) -> list[str]:
    """Split long text on line boundaries into <=``limit`` chunks."""
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    current = ""
    for line in text.split("\n"):
        candidate = line if not current else f"{current}\n{line}"
        if len(candidate) <= limit:
            current = candidate
        else:
            if current:
                parts.append(current)
            # A single over-long line is hard-split.
            while len(line) > limit:
                parts.append(line[:limit])
                line = line[limit:]
            current = line
    if current:
        parts.append(current)
    return parts


def build_caption_text(concept: str, hook: str, content: Content) -> str:
    """Compose the full preview text: concept, hook, 3 captions, hashtags."""
    caps = content.captions
    return (
        f"📣 {concept}\n"
        f"{hook}\n\n"
        f"— LinkedIn —\n{caps.linkedin}\n\n"
        f"— Instagram —\n{caps.instagram}\n\n"
        f"— X —\n{caps.x}\n\n"
        f"{' '.join(content.hashtags)}"
    )


def encode_callback(action: str, post_id: str) -> str:
    return f"{_ACTION_CODE.get(action, action)}|{post_id}"


def parse_callback(data: str) -> tuple[str, str]:
    code, _, post_id = data.partition("|")
    return _CODE_ACTION.get(code, code), post_id


def action_keyboard(post_id: str) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "✅ Approve Now", "callback_data": encode_callback("approve", post_id)},
                {"text": "⏰ Schedule", "callback_data": encode_callback("approve_schedule", post_id)},
            ],
            [
                {"text": "❌ Reject", "callback_data": encode_callback("reject", post_id)},
                {"text": "🔄 Regenerate", "callback_data": encode_callback("regen", post_id)},
            ]
        ]
    }


def schedule_marker(post_id: str) -> str:
    return _SCHEDULE_MARKER.format(id=post_id)


def parse_schedule_marker(text: str) -> str | None:
    """Extract the post id from a ForceReply prompt's embedded marker."""
    start = text.find("[schedule:")
    if start == -1:
        return None
    end = text.find("]", start)
    if end == -1:
        return None
    return text[start + len("[schedule:") : end].strip() or None


def regen_marker(post_id: str) -> str:
    return _REGEN_MARKER.format(id=post_id)


def parse_regen_marker(text: str) -> str | None:
    """Extract the post id from a ForceReply prompt's embedded marker."""
    start = text.find("[regen:")
    if start == -1:
        return None
    end = text.find("]", start)
    if end == -1:
        return None
    return text[start + len("[regen:") : end].strip() or None


# --------------------------------------------------------------------------- #
# Telegram transport
# --------------------------------------------------------------------------- #
class TelegramAPI(Protocol):
    """The slice of the Bot API the approval service depends on."""

    def send_media_group(self, chat_id: str, photo_paths: list[Path], caption: str | None) -> Any: ...
    def send_message(self, chat_id: str, text: str, reply_markup: dict | None = None) -> dict: ...
    def send_document(self, chat_id: str, document_path: Path, caption: str | None = None) -> Any: ...
    def edit_message_text(self, chat_id: str, message_id: int, text: str) -> Any: ...
    def answer_callback_query(self, callback_query_id: str, text: str | None = None) -> Any: ...
    def get_updates(self, offset: int | None, timeout: int) -> list[dict]: ...


class RetryableTelegramError(RuntimeError):
    """A transient Telegram error (429 / 5xx) worth retrying."""


_net_retry = retry(
    retry=retry_if_exception_type((httpx.TransportError, RetryableTelegramError)),
    wait=wait_exponential(multiplier=1, min=2, max=20),
    stop=stop_after_attempt(3),
    reraise=True,
)


class TelegramClient:
    """Thin synchronous Bot API client over httpx."""

    def __init__(self, token: str) -> None:
        if not token:
            raise ApprovalError("TELEGRAM_BOT_TOKEN is not set")
        self._base = f"https://api.telegram.org/bot{token}"

    def _check(self, resp: httpx.Response) -> Any:
        # Surface Telegram's own "description" instead of a bare status code, and
        # classify 429/5xx as retryable, other 4xx as permanent.
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001
            body = {}
        if resp.status_code >= 400 or not body.get("ok"):
            desc = body.get("description") or resp.text[:200]
            msg = f"telegram HTTP {resp.status_code}: {desc}"
            if resp.status_code == 429 or resp.status_code >= 500:
                raise RetryableTelegramError(msg)
            raise ApprovalError(msg)
        return body.get("result")

    @_net_retry
    def _post(self, method: str, payload: dict[str, Any]) -> Any:
        with httpx.Client(timeout=httpx.Timeout(70.0, connect=10.0)) as client:
            resp = client.post(f"{self._base}/{method}", json=payload)
            return self._check(resp)

    @_net_retry
    def _post_multipart(self, method: str, data: dict[str, Any], files: dict) -> Any:
        with httpx.Client(timeout=httpx.Timeout(120.0, connect=10.0)) as client:
            resp = client.post(f"{self._base}/{method}", data=data, files=files)
            return self._check(resp)

    def send_media_group(self, chat_id: str, photo_paths: list[Path], caption: str | None) -> Any:
        media: list[dict[str, Any]] = []
        files: dict[str, Any] = {}
        for i, path in enumerate(photo_paths):
            key = f"photo{i}"
            item: dict[str, Any] = {"type": "photo", "media": f"attach://{key}"}
            if i == 0 and caption:
                item["caption"] = caption
            media.append(item)
            files[key] = (path.name, path.read_bytes(), "image/png")
        return self._post_multipart(
            "sendMediaGroup", {"chat_id": chat_id, "media": json.dumps(media)}, files
        )

    def send_message(self, chat_id: str, text: str, reply_markup: dict | None = None) -> dict:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return self._post("sendMessage", payload)

    def send_document(self, chat_id: str, document_path: Path, caption: str | None = None) -> Any:
        data: dict[str, Any] = {"chat_id": chat_id}
        if caption:
            data["caption"] = caption
        files = {
            "document": (document_path.name, document_path.read_bytes(), "application/pdf")
        }
        return self._post_multipart("sendDocument", data, files)

    def edit_message_text(self, chat_id: str, message_id: int, text: str) -> Any:
        return self._post(
            "editMessageText",
            {"chat_id": chat_id, "message_id": message_id, "text": text},
        )

    def answer_callback_query(self, callback_query_id: str, text: str | None = None) -> Any:
        # Best-effort and single-shot: a callback answer only dismisses the
        # button spinner / shows a toast, and Telegram rejects it once the query
        # is older than ~15s. Retrying a stale query always fails and would block
        # the poll loop, so we fire once and swallow any error.
        payload: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        try:
            with httpx.Client(timeout=httpx.Timeout(15.0, connect=10.0)) as client:
                client.post(f"{self._base}/answerCallbackQuery", json=payload)
        except Exception as exc:  # noqa: BLE001
            logger.debug("answerCallbackQuery best-effort failed: %s", exc)
        return None

    def get_updates(self, offset: int | None, timeout: int) -> list[dict]:
        payload: dict[str, Any] = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            payload["offset"] = offset
        return self._post("getUpdates", payload) or []

    def set_webhook(self, url: str, secret_token: str) -> Any:
        return self._post(
            "setWebhook",
            {
                "url": url,
                "secret_token": secret_token,
                "allowed_updates": ["message", "callback_query"],
            },
        )

    def delete_webhook(self, drop_pending: bool = False) -> Any:
        return self._post("deleteWebhook", {"drop_pending_updates": drop_pending})


# --------------------------------------------------------------------------- #
# Approval service
# --------------------------------------------------------------------------- #
# regen_runner(parent_id, concept_override) -> new post id (already rendered)
RegenRunner = Callable[[str, str | None], str]


class ApprovalService:
    """Drives the Telegram approval flow against the SQLite state machine."""

    def __init__(
        self,
        telegram: TelegramAPI,
        store: StateStore,
        settings: Settings,
        sync: SupabaseSync,
        regen_runner: RegenRunner,
        publish_runner: Callable[[str], Any] | None = None,
        mark_linkedin_runner: Callable[[str], Any] | None = None,
    ) -> None:
        self._tg = telegram
        self._store = store
        self._settings = settings
        self._sync = sync
        self._regen = regen_runner
        # publish_runner(post_id) publishes an approved, unscheduled post
        # immediately after the Approve tap (Phase 4); None disables that hook.
        self._publish = publish_runner
        # mark_linkedin_runner(post_id) marks LinkedIn posted *and* recomputes
        # the overall state (COMPLETE when every enabled platform is done).
        self._mark_linkedin = mark_linkedin_runner
        self._chat_id = settings.telegram_admin_chat_id
        self._admin_chat_ids = settings.telegram_admin_chat_ids

    def _broadcast(self, text: str, exclude: Any = None) -> None:
        """Send ``text`` to every admin (optionally skipping the actor)."""
        for admin in self._admin_chat_ids:
            if exclude is not None and str(admin) == str(exclude):
                continue
            try:
                self._tg.send_message(admin, text)
            except Exception as exc:  # noqa: BLE001
                logger.warning("broadcast to admin %s failed: %s", admin, exc)

    # -- preview -------------------------------------------------------------
    def send_preview(self, post_id: str, *, degraded: bool = False) -> None:
        """Send album + captions + action buttons to ALL admin chats and move to AWAITING_APPROVAL.

        ``degraded`` prepends a visible warning to the action prompt: the render
        fell back to gradients for too many slides. Nothing auto-publishes — the
        post still waits behind the approval gate — but the admin is told so they
        can Reject/Regenerate instead of approving low-quality visuals.
        """
        record = self._store.get_post(post_id)
        if record is None:
            raise ApprovalError(f"unknown post id {post_id!r}")
        content = self._load_content(post_id)
        brief = self._load_brief(post_id)
        slides = self._slide_paths(post_id)
        if not slides:
            raise ApprovalError(f"no rendered slides for {post_id!r}; render it first")

        caption_text = build_caption_text(
            brief.get("concept", record.concept), brief.get("hook", ""), content
        )
        fits = len(caption_text) <= CAPTION_LIMIT
        action_prompt = "Choose an action for this post:"
        if degraded:
            action_prompt = (
                "⚠️ DEGRADED: too many slides fell back to the gradient (check image "
                "provider keys/quota). Reject or Regenerate unless this is acceptable.\n\n"
                + action_prompt
            )

        # Broadcast to ALL admin chat IDs
        for admin_chat_id in self._admin_chat_ids:
            try:
                for i, group in enumerate(chunk_list(slides, MEDIA_GROUP_MAX)):
                    cap = caption_text if (i == 0 and fits) else None
                    self._tg.send_media_group(admin_chat_id, group, cap)

                if not fits:
                    for part in split_text(caption_text, TEXT_LIMIT):
                        self._tg.send_message(admin_chat_id, part)

                self._tg.send_message(
                    admin_chat_id,
                    action_prompt,
                    reply_markup=action_keyboard(post_id),
                )
                logger.info("preview sent to chat=%s id=%s", admin_chat_id, post_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("failed to send preview to chat=%s: %s", admin_chat_id, exc)

        if record.state == PostState.DRAFTED:
            record = self._store.transition(post_id, PostState.AWAITING_APPROVAL)
        self._sync.push_state(record, content)
        logger.info("preview broadcast complete id=%s state=%s", post_id, record.state.value)

    # -- update dispatch -----------------------------------------------------
    def handle_update(self, update: dict[str, Any]) -> None:
        if "callback_query" in update:
            self._handle_callback(update["callback_query"])
        elif "message" in update:
            self._handle_message(update["message"])

    def _is_admin(self, chat_id: Any) -> bool:
        # Support comma-separated chat IDs in env var
        return str(chat_id) in self._admin_chat_ids

    def _handle_callback(self, cq: dict[str, Any]) -> None:
        message = cq.get("message", {})
        chat_id = message.get("chat", {}).get("id")
        if not self._is_admin(chat_id):
            logger.warning("ignoring callback from non-admin chat=%s", chat_id)
            return

        action, post_id = parse_callback(cq.get("data", ""))
        cq_id = cq.get("id")
        message_id = message.get("message_id")
        record = self._store.get_post(post_id)

        if record is None or action not in ACTIONS:
            self._tg.answer_callback_query(cq_id, "Unknown or expired post.")
            return

        # LinkedIn Mark-posted lives outside the approval gate (the post is
        # already deep into publishing when this button exists).
        if action == "linkedin_done":
            self._handle_linkedin_done(cq_id, chat_id, message_id, record)
            return

        # Idempotency across admins: only an AWAITING_APPROVAL post is
        # actionable; the first tap wins and later taps learn who acted.
        if record.state != PostState.AWAITING_APPROVAL:
            handler = record.handled_by or "another admin"
            self._tg.answer_callback_query(cq_id, f"Already handled by {handler}.")
            return

        now = datetime.now().strftime("%H:%M")
        if action == "approve":
            # Answer first (it expires fast), then do the durable work + edit.
            self._tg.answer_callback_query(cq_id, "Approved ✅")
            self._store.set_handled_by(post_id, str(chat_id))
            updated = self._safe_transition(post_id, PostState.APPROVED)
            self._tg.edit_message_text(
                chat_id,
                message_id,
                f"✅ Approved at {now}\n\nNo schedule set — publishing now.",
            )
            self._sync.push_state(updated)
            self._broadcast(f"✅ {post_id} approved by {chat_id}", exclude=chat_id)
            self._publish_now(post_id)
        elif action == "approve_schedule":
            self._prompt_schedule(cq_id, chat_id, post_id)
        elif action == "reject":
            self._tg.answer_callback_query(cq_id, "Rejected")
            self._store.set_handled_by(post_id, str(chat_id))
            updated = self._safe_transition(post_id, PostState.REJECTED)
            self._tg.edit_message_text(chat_id, message_id, f"❌ Rejected at {now}")
            self._sync.push_state(updated)
            self._broadcast(f"❌ {post_id} rejected by {chat_id}", exclude=chat_id)
        elif action == "regen":
            self._prompt_regen(cq_id, chat_id, record.date, post_id)

    def _publish_now(self, post_id: str) -> None:
        """Publish immediately after an unscheduled Approve (Phase 4 hook)."""
        if self._publish is None:
            return
        record = self._store.get_post(post_id)
        if record is None or record.scheduled_time is not None:
            return
        try:
            self._publish(post_id)  # the publish service reports results to all admins
        except Exception as exc:  # noqa: BLE001 - publishing must not kill the bot loop
            logger.error("immediate publish failed id=%s: %s", post_id, exc)
            self._broadcast(f"⚠️ Publishing {post_id} failed: {exc}\nRetry with: publish {post_id}")

    def _handle_linkedin_done(
        self, cq_id: Any, chat_id: Any, message_id: Any, record
    ) -> None:
        """Mark LinkedIn manually posted — idempotent across admins."""
        if record.linkedin_status == "posted":
            self._tg.answer_callback_query(cq_id, "Already marked posted.")
            return
        self._tg.answer_callback_query(cq_id, "LinkedIn marked posted ✅")
        if self._mark_linkedin is not None:
            updated = self._mark_linkedin(record.id)
        else:
            self._store.set_platform_result(record.id, "linkedin", "posted")
            updated = self._store.get_post(record.id)
            self._sync.push_state(updated)
        self._tg.edit_message_text(
            chat_id, message_id, f"💼 LinkedIn marked posted for {record.id} ✅"
        )
        note = f"💼 {record.id}: LinkedIn marked posted by {chat_id}"
        if updated is not None and updated.state is PostState.COMPLETE:
            note += "\n🏁 All enabled platforms done — post COMPLETE."
        self._broadcast(note, exclude=chat_id)

    def _prompt_regen(self, cq_id: Any, chat_id: Any, date: str, post_id: str) -> None:
        used = self._store.count_regenerations(date)
        if used >= self._settings.max_regenerations_per_day:
            self._tg.answer_callback_query(
                cq_id, "Max regenerations reached — please Approve or Reject."
            )
            return
        # Answer first (the slower ForceReply send must not push us past the
        # callback's ~15s expiry window).
        self._tg.answer_callback_query(cq_id, "Send a topic…")
        prompt = f"{REGEN_PROMPT}\n\n{regen_marker(post_id)}"
        self._tg.send_message(
            chat_id,
            prompt,
            reply_markup={"force_reply": True, "input_field_placeholder": "topic or auto"},
        )

    def _prompt_schedule(self, cq_id: Any, chat_id: Any, post_id: str) -> None:
        """Prompt admin to send a scheduled time (IST by default)."""
        self._tg.answer_callback_query(cq_id, "Send posting time…")
        prompt = (
            "⏰ Send the posting time in IST:\n"
            "  2026-06-13 18:00\n"
            "(or explicit UTC: 2026-06-13T12:30:00Z)\n\n"
            f"{schedule_marker(post_id)}"
        )
        self._tg.send_message(
            chat_id,
            prompt,
            reply_markup={"force_reply": True, "input_field_placeholder": "YYYY-MM-DD HH:MM (IST)"},
        )

    def _handle_schedule_reply(self, chat_id: Any, post_id: str, text: str) -> None:
        """Store the typed schedule time (IST → UTC) and approve the post."""
        from gelio.timeutil import ScheduleParseError, parse_schedule_input, utc_to_ist

        record = self._store.get_post(post_id)
        if record is None:
            self._tg.send_message(chat_id, "That post no longer exists.")
            return
        if record.state != PostState.AWAITING_APPROVAL:
            handler = record.handled_by or "another admin"
            self._tg.send_message(chat_id, f"Already handled by {handler}.")
            return
        try:
            scheduled_utc = parse_schedule_input(text)
        except (ScheduleParseError, ValueError) as exc:
            self._tg.send_message(
                chat_id,
                f"❌ Couldn't parse that time: {exc}\n"
                "Send IST like `2026-06-13 18:00`, or tap ⏰ Schedule again.",
            )
            return
        self._store.set_scheduled_time(post_id, scheduled_utc)
        self._store.set_handled_by(post_id, str(chat_id))
        updated = self._safe_transition(post_id, PostState.APPROVED)
        self._sync.push_state(updated)
        ist = utc_to_ist(scheduled_utc).strftime("%Y-%m-%d %I:%M %p IST")
        self._tg.send_message(
            chat_id, f"✅ Approved and scheduled {post_id} for {ist} ({scheduled_utc} UTC)."
        )
        self._broadcast(
            f"⏰ {post_id} approved & scheduled for {ist} by {chat_id}", exclude=chat_id
        )
        logger.info("scheduled id=%s utc=%s by admin=%s", post_id, scheduled_utc, chat_id)

    def _handle_start_command(self, chat_id: Any) -> None:
        """Handle /start command - generate new post and send for approval to ALL admins."""
        logger.info("/start command received from chat=%s", chat_id)
        
        # Send "generating" message to the user who triggered it
        self._tg.send_message(
            chat_id,
            "🚀 Generating new post...\n\n"
            "⏳ This will take ~30-60 seconds:\n"
            "  • Selecting concept\n"
            "  • Writing content\n"
            "  • Rendering slides\n"
            "  • Building PDF\n\n"
            "Please wait..."
        )
        
        try:
            # Import pipeline here to avoid circular dependency
            from gelio.pipeline import build_pipeline
            
            pipeline, store = build_pipeline(self._settings)
            try:
                # Generate with render enabled
                result = pipeline.generate(
                    slides=self._settings.default_slides,
                    dry_run=False,
                    render=True,
                    force=False,
                )
                
                # Send success/info message to ALL admins
                if result.already_existed:
                    msg = (
                        f"ℹ️ Post already exists: {result.brief.id}\n\n"
                        f"Concept: {result.brief.concept}\n\n"
                        "Sending existing post for approval..."
                    )
                else:
                    msg = (
                        f"✅ Post generated by admin {chat_id}!\n\n"
                        f"📋 ID: {result.brief.id}\n"
                        f"💡 Concept: {result.brief.concept}\n"
                        f"🎨 Slides: {len(result.content.slides)}\n\n"
                        "Sending for approval..."
                    )
                
                # Notify all admins
                for admin_id in self._admin_chat_ids:
                    try:
                        self._tg.send_message(admin_id, msg)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("failed to notify admin %s: %s", admin_id, exc)
                
                # Send preview to ALL admins (this already broadcasts)
                self.send_preview(result.brief.id)
                
                logger.info(
                    "/start generated and broadcast id=%s concept=%s to %d admins",
                    result.brief.id,
                    result.brief.concept,
                    len(self._admin_chat_ids)
                )
                
            finally:
                store.close()
                
        except Exception as exc:  # noqa: BLE001
            logger.error("/start command failed: %s", exc, exc_info=True)
            # Send error to the user who triggered it
            self._tg.send_message(
                chat_id,
                f"❌ Generation failed: {exc}\n\n"
                "Please check the server logs or try again with /start"
            )

    def _handle_message(self, message: dict[str, Any]) -> None:
        chat_id = message.get("chat", {}).get("id")
        if not self._is_admin(chat_id):
            logger.warning("ignoring message from non-admin chat=%s", chat_id)
            return

        # Handle /start command
        text = (message.get("text") or "").strip()
        if text == "/start":
            self._handle_start_command(chat_id)
            return

        reply_to = message.get("reply_to_message")
        if not reply_to:
            return  # not a ForceReply answer we care about

        # Schedule reply: "[schedule:<id>]" prompt answered with an IST time.
        schedule_id = parse_schedule_marker(reply_to.get("text", ""))
        if schedule_id is not None:
            self._handle_schedule_reply(chat_id, schedule_id, text)
            return

        parent_id = parse_regen_marker(reply_to.get("text", ""))
        if parent_id is None:
            return

        parent = self._store.get_post(parent_id)
        if parent is None:
            self._tg.send_message(chat_id, "That post no longer exists.")
            return

        # Re-check the daily cap at answer time.
        if self._store.count_regenerations(parent.date) >= self._settings.max_regenerations_per_day:
            self._tg.send_message(
                chat_id, "Max regenerations reached — please Approve or Reject the latest version."
            )
            return

        topic = (message.get("text") or "").strip()
        concept_override = None if topic.lower() == "auto" else topic

        # Reject the predecessor, then spawn the regenerated post.
        self._safe_transition(parent_id, PostState.REJECTED)
        self._tg.send_message(
            chat_id,
            f"♻️ Regenerating "
            + (f"on “{topic}”…" if concept_override else "with an auto-picked topic…"),
        )
        try:
            new_id = self._regen(parent_id, concept_override)
        except Exception as exc:  # noqa: BLE001
            logger.error("regeneration failed parent=%s: %s", parent_id, exc)
            self._tg.send_message(chat_id, f"Regeneration failed: {exc}")
            return
        self.send_preview(new_id)

    def _safe_transition(self, post_id: str, new_state: PostState):
        """Transition, tolerating an already-applied target state."""
        record = self._store.get_post(post_id)
        if record is not None and record.state == new_state:
            return record
        try:
            return self._store.transition(post_id, new_state)
        except StateTransitionError as exc:
            logger.warning("transition skipped id=%s -> %s: %s", post_id, new_state.value, exc)
            return self._store.get_post(post_id)

    # -- long-poll loop ------------------------------------------------------
    def run_bot(self, poll_timeout: int = 30, _max_iters: int | None = None) -> None:
        """Long-poll Telegram and dispatch updates until interrupted."""
        logger.info("bot started; long-polling for admin chat=%s", self._chat_id)
        offset: int | None = None
        iters = 0
        try:
            while _max_iters is None or iters < _max_iters:
                iters += 1
                try:
                    updates = self._tg.get_updates(offset, timeout=poll_timeout)
                except Exception as exc:  # noqa: BLE001 - keep the loop alive
                    logger.warning("getUpdates failed: %s", exc)
                    time.sleep(3)
                    continue
                for update in updates:
                    offset = update["update_id"] + 1
                    try:
                        self.handle_update(update)
                    except Exception as exc:  # noqa: BLE001 - one bad update won't kill the bot
                        logger.error("error handling update: %s", exc)
        except KeyboardInterrupt:
            logger.info("bot stopped by user")

    # -- artifact loading ----------------------------------------------------
    def _slide_paths(self, post_id: str) -> list[Path]:
        slides_dir = self._settings.output_dir / post_id / "slides"
        if not slides_dir.exists():
            return []
        return sorted(
            slides_dir.glob("slide_*.png"),
            key=lambda p: int(p.stem.split("_")[1]),
        )

    def _load_content(self, post_id: str) -> Content:
        path = self._settings.output_dir / post_id / "content.json"
        return Content.model_validate(json.loads(path.read_text(encoding="utf-8")))

    def _load_brief(self, post_id: str) -> dict[str, Any]:
        path = self._settings.output_dir / post_id / "brief.json"
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))


def build_approval(settings: Settings, store: StateStore, sync: SupabaseSync) -> ApprovalService:
    """Construct an ApprovalService with lazily-built regen + publish runners.

    The regen runner builds a full pipeline (LLM + render) only when a
    regeneration actually happens, so ``send-approval`` works without LLM keys.
    The publish runners share this Telegram client, so LinkedIn delivery and
    result broadcasts reach the same admin chats.
    """
    telegram = TelegramClient(settings.telegram_bot_token)

    def _regen(parent_id: str, concept_override: str | None) -> str:
        from gelio.pipeline import build_pipeline  # local import avoids a cycle
        from gelio.topic_engine import parse_series_request

        # A typed topic shaped like "10 Steps to ..." (or "series: <title>")
        # regenerates as a numbered series instead of a plain carousel.
        series_title, series_slides = parse_series_request(concept_override or "")
        pipeline, pstore = build_pipeline(settings)
        try:
            parent = store.get_post(parent_id)
            rc = (parent.regeneration_count if parent else 0) + 1
            result = pipeline.generate(
                slides=series_slides or settings.default_slides,
                render=True,
                concept_override=None if series_title else concept_override,
                series_title=series_title,
                parent_id=parent_id,
                regeneration_count=rc,
            )
            return result.brief.id
        finally:
            pstore.close()

    def _publish_service():
        from gelio.publisher import build_publish_service  # local import avoids a cycle

        return build_publish_service(settings, store, sync, telegram=telegram)

    def _publish(post_id: str):
        return _publish_service().publish(post_id)

    def _mark_linkedin(post_id: str):
        return _publish_service().mark_linkedin_posted(post_id)

    return ApprovalService(
        telegram, store, settings, sync, _regen,
        publish_runner=_publish, mark_linkedin_runner=_mark_linkedin,
    )
