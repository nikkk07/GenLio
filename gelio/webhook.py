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
from typing import Any

from fastapi import FastAPI, Header, Request, Response

from gelio.approval import ApprovalService

logger = logging.getLogger("gelio.webhook")

_SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"


def _valid(provided: str | None, expected: str) -> bool:
    """Constant-time secret comparison (never True for an empty expected)."""
    if not expected or not provided:
        return False
    return hmac.compare_digest(provided, expected)


def create_app(approval: ApprovalService, secret: str) -> FastAPI:
    """Build the webhook app around an already-constructed approval service.

    Kept separate from :func:`build_app` so tests can inject a fake approval
    service and exercise routing + secret validation without any network.
    """
    app = FastAPI(title="gelio telegram webhook", docs_url=None, redoc_url=None)

    @app.get("/health")
    def health() -> dict[str, Any]:
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

        update = await request.json()
        try:
            # Same handler as long-poll: admin check, approve→publish-inline,
            # reject, regenerate, schedule — all reused, not reimplemented.
            approval.handle_update(update)
        except Exception as exc:  # noqa: BLE001 - never 500 (avoids Telegram retry storms)
            logger.error("error handling webhook update: %s", exc)
        # Always 200 fast so Telegram marks the update delivered.
        return Response(status_code=200, content='{"ok":true}', media_type="application/json")

    return app


def build_app() -> FastAPI:
    """Wire the production approval service from environment settings.

    Imported by the serverless entrypoint (``api/telegram.py``). Reads the
    Telegram token, Supabase keys, and publish keys from the host environment.
    """
    from config.settings import load_settings
    from gelio.approval import build_approval
    from gelio.store import build_store
    from gelio.sync import build_sync

    settings = load_settings()
    store = build_store(settings)
    sync = build_sync(settings)
    approval = build_approval(settings, store, sync)
    return create_app(approval, settings.telegram_webhook_secret)
