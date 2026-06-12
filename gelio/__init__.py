"""gelio — a free-tier AI content agent for We One Aviation.

Phase 1 (Content Intelligence Core): turn a psychology concept into a
schema-locked carousel Brief + Content artifact, validated and persisted.
"""

from __future__ import annotations

import logging

__version__ = "0.1.0"


def configure_logging(level: int = logging.INFO) -> None:
    """Configure root logging with a compact, JSON-ish single-line format.

    Idempotent: repeated calls do not stack handlers.
    """
    from gelio.redact import TokenRedactionFilter

    root = logging.getLogger("gelio")
    if root.handlers:
        return
    handler = logging.StreamHandler()
    handler.addFilter(TokenRedactionFilter())
    handler.setFormatter(
        logging.Formatter(
            '{"ts":"%(asctime)s","level":"%(levelname)s",'
            '"logger":"%(name)s","msg":"%(message)s"}'
        )
    )
    root.addHandler(handler)
    root.setLevel(level)
    root.propagate = False
