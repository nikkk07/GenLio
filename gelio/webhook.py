"""Telegram webhook — the free, no-always-on-server approval path.

In production gelio runs from an ephemeral GitHub Actions cron (generate +
publish-due), so there is no long-running process to receive an admin's
approval tap minutes or hours later. Instead Telegram POSTs each update to a
serverless function (Vercel / Supabase Edge) that hosts this FastAPI app, which
routes the update to the *same* :class:`~gelio.approval.ApprovalService` the
long-poll bot uses — no forked logic.

Security: the endpoint path embeds a secret (``/telegram/{secret}``) AND
Telegram echoes a configured secret in the ``X-Telegram-Bot-Api-Secret-Token``
header (set via ``setWebhook``). Both must match the configured
``TELEGRAM_WEBHOOK_SECRET`` or the request is rejected, so a leaked URL alone
can't drive the bot. Admin filtering is unchanged — it happens inside
``handle_update`` exactly as in long-poll mode.
"""

from __future__ import annotations

import hmac
import logging
from typing import Any, Callable, Union

from fastapi import FastAPI, Header, Request, Response

from gelio.approval import ApprovalService

logger = logging.getLogger("gelio.webhook")

_SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"

# create_app accepts either a ready service (tests) or a lazy factory (prod, so
# a misconfigured deploy fails per-request instead of crashing app import).
ApprovalProvider = Union[ApprovalService, Callable[[], ApprovalService]]


class WebhookConfigError(RuntimeError):
    """Raised when the serverless webhook is misconfigured (e.g. not Supabase)."""


def require_supabase_state(settings) -> None:
    """The serverless webhook has no writable disk, so SQLite can't work.

    Demand ``GELIO_STATE=supabase`` plus Supabase credentials and raise a clear,
    actionable error otherwise — instead of letting ``build_store`` try to open
    a local DB file and crash with ``unable to open database file``.
    """
    if settings.state_backend != "supabase":
        raise WebhookConfigError(
            "Webhook requires GELIO_STATE=supabase + Supabase keys "
            f"(got GELIO_STATE={settings.state_backend!r}). Serverless functions "
            "have no writable disk, so the SQLite backend cannot be used."
        )
    if not settings.supabase_url or not settings.supabase_service_key:
        raise WebhookConfigError(
            "Webhook requires GELIO_STATE=supabase + Supabase keys "
            "(SUPABASE_URL and SUPABASE_SERVICE_KEY must be set)."
        )


def _valid(provided: str | None, expected: str) -> bool:
    """Constant-time secret comparison (never True for an empty expected)."""
    if not expected or not provided:
        return False
    return hmac.compare_digest(provided, expected)


def create_app(approval: ApprovalProvider, secret: str) -> FastAPI:
    """Build the webhook app around an approval service or a lazy factory.

    ``approval`` may be a ready :class:`ApprovalService` (tests inject a fake) or
    a zero-arg factory (production). The factory is called on the FIRST webhook
    request and cached — never at app import — so ``/health`` stays available
    even when the deploy is misconfigured (no store is touched until a real
    update arrives). A :class:`WebhookConfigError` from the factory becomes a
    clear ``503`` instead of a crashed import.
    """
    app = FastAPI(title="gelio telegram webhook", docs_url=None, redoc_url=None)
    _cache: dict[str, ApprovalService] = {}

    def _get_approval() -> ApprovalService:
        svc = _cache.get("svc")
        if svc is None:
            svc = approval() if callable(approval) else approval
            _cache["svc"] = svc
        return svc

    @app.get("/health")
    def health() -> dict[str, Any]:
        # Deliberately does NOT build the store/approval service — a health
        # check must work even if state is misconfigured.
        return {"ok": True, "configured": bool(secret)}

    @app.post("/telegram/{path_secret}")
    async def telegram(
        path_secret: str,
        request: Request,
        x_secret: str | None = Header(default=None, alias=_SECRET_HEADER),
    ) -> Response:
        # Both the unguessable path segment and Telegram's echoed header must
        # match the configured secret. Reject spoofed POSTs with 403.
        if not _valid(path_secret, secret) or not _valid(x_secret, secret):
            logger.warning("rejected webhook call: bad secret")
            return Response(status_code=403, content="forbidden")

        try:
            approval_svc = _get_approval()
        except WebhookConfigError as exc:
            # Misconfigured deploy (e.g. SQLite on serverless): surface a clear
            # 503 so the operator sees the cause; Telegram will retry once fixed.
            logger.error("webhook misconfigured: %s", exc)
            return Response(
                status_code=503, content=str(exc), media_type="text/plain"
            )

        update = await request.json()
        try:
            # Same handler as long-poll: admin check, approve→publish-inline,
            # reject, regenerate, schedule — all reused, not reimplemented.
            approval_svc.handle_update(update)
        except Exception as exc:  # noqa: BLE001 - never 500 (avoids Telegram retry storms)
            logger.error("error handling webhook update: %s", exc)
        # Always 200 fast so Telegram marks the update delivered.
        return Response(status_code=200, content='{"ok":true}', media_type="application/json")

    return app


def build_app() -> FastAPI:
    """Wire the production approval service from environment settings.

    Imported by the serverless entrypoint (``api/telegram.py``). The approval
    service (and therefore the Supabase store) is built lazily on the first
    update, so importing this module never opens a DB — ``/health`` works even
    when state is misconfigured, and a non-Supabase deploy fails with a clear
    :class:`WebhookConfigError` (503) rather than an ``unable to open database
    file`` crash.
    """
    from config.settings import load_settings

    settings = load_settings()

    def _factory() -> ApprovalService:
        from gelio.approval import build_approval
        from gelio.store import build_store
        from gelio.sync import build_sync

        require_supabase_state(settings)  # serverless has no writable disk
        store = build_store(settings)
        sync = build_sync(settings)
        return build_approval(settings, store, sync)

    return create_app(_factory, settings.telegram_webhook_secret)
