"""Token redaction: no log line may ever contain the bot token."""

from __future__ import annotations

import logging

from gelio.redact import REDACTED, TokenRedactionFilter, redact

TOKEN = "7215079511:AAH8fakefakefakefakefakefakefakeXYZ"


def test_redact_bot_url():
    line = f"POST https://api.telegram.org/bot{TOKEN}/sendMessage failed"
    out = redact(line)
    assert TOKEN not in out
    assert "api.telegram.org/bot***/sendMessage" in out


def test_redact_bare_token():
    out = redact(f"token is {TOKEN} ok")
    assert TOKEN not in out and REDACTED in out


def test_redact_extra_secrets():
    out = redact("Bearer sbsecretkey123", secrets=["sbsecretkey123"])
    assert "sbsecretkey123" not in out


def test_redact_leaves_normal_text_alone():
    line = "transition id=2026-06-12-foo APPROVED -> POSTED_X"
    assert redact(line) == line


def test_logging_filter_scrubs_msg_and_args(monkeypatch, caplog):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    logger = logging.getLogger("gelio.test_redact")
    logger.addFilter(TokenRedactionFilter())
    with caplog.at_level(logging.INFO, logger="gelio.test_redact"):
        logger.info("url https://api.telegram.org/bot%s/getUpdates broke", TOKEN)
        logger.info("exception: %s", RuntimeError(f"connect to bot{TOKEN} failed"))
    for record in caplog.records:
        assert TOKEN not in record.getMessage()


def test_logging_filter_keeps_non_string_args_intact(caplog):
    logger = logging.getLogger("gelio.test_redact2")
    logger.addFilter(TokenRedactionFilter())
    with caplog.at_level(logging.INFO, logger="gelio.test_redact2"):
        logger.info("count=%d", 42)
    assert caplog.records[0].getMessage() == "count=42"
