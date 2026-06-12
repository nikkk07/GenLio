"""Secret redaction for log lines and exception text.

The Telegram Bot API embeds the bot token in every request URL
(``api.telegram.org/bot<token>/method``), so any logged URL or httpx exception
string is a potential leak. ``redact()`` scrubs both the URL pattern and any
literal secret values, and ``TokenRedactionFilter`` enforces it on every record
emitted through the ``gelio`` logger tree — modules don't have to remember to
call it.
"""

from __future__ import annotations

import logging
import os
import re

# Matches the token segment of a Bot API URL: /bot<digits>:<base64ish>.
_BOT_URL_RE = re.compile(r"bot\d{6,}:[A-Za-z0-9_-]{20,}")
# A bare BotFather token outside a URL.
_BARE_TOKEN_RE = re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{30,}\b")

REDACTED = "***"


def redact(text: str, secrets: list[str] | None = None) -> str:
    """Return ``text`` with bot tokens and any extra ``secrets`` masked."""
    out = _BOT_URL_RE.sub(f"bot{REDACTED}", text)
    out = _BARE_TOKEN_RE.sub(REDACTED, out)
    for secret in secrets or []:
        if secret:
            out = out.replace(secret, REDACTED)
    return out


def _env_secrets() -> list[str]:
    """Secrets worth scrubbing wholesale if they ever reach a log line."""
    keys = (
        "TELEGRAM_BOT_TOKEN",
        "SUPABASE_SERVICE_KEY",
        "X_CONSUMER_SECRET",
        "X_ACCESS_TOKEN_SECRET",
        "X_ACCESS_TOKEN",
        "IG_ACCESS_TOKEN",
    )
    return [v for k in keys if (v := os.getenv(k, "").strip())]


class TokenRedactionFilter(logging.Filter):
    """Scrub secrets from every record before it is formatted."""

    def filter(self, record: logging.LogRecord) -> bool:
        secrets = _env_secrets()
        record.msg = redact(str(record.msg), secrets)
        if record.args:
            # Exceptions and other objects are rendered via %s at format time,
            # so scrub their string form too; clean args pass through unchanged
            # (keeping ints as ints for %d).
            record.args = tuple(
                cleaned if (cleaned := redact(str(a), secrets)) != str(a) else a
                for a in record.args
            )
        return True
