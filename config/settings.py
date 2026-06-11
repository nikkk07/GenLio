"""Typed application settings loaded from environment variables and config files.

Settings are read once from the process environment (populated from a local
``.env`` file via ``python-dotenv``) plus the JSON brand kit. Nothing here
contains secrets at rest — keys live only in ``.env`` and are read on demand.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
import os

logger = logging.getLogger("gelio.settings")

# Project root = parent of the ``config`` package directory.
ROOT_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT_DIR / "config"
DATA_DIR = ROOT_DIR / "data"
OUTPUT_DIR = ROOT_DIR / "output"
ASSETS_DIR = ROOT_DIR / "assets"
FONTS_DIR = ASSETS_DIR / "fonts"

BRAND_PATH = CONFIG_DIR / "brand.json"
TOPIC_BANK_PATH = DATA_DIR / "topic_bank.json"
DB_PATH = DATA_DIR / "gelio.db"

# Load .env (no-op if the file is absent; real env vars still win).
load_dotenv(ROOT_DIR / ".env")


class ConfigError(RuntimeError):
    """Raised when configuration is missing or malformed."""


@dataclass(frozen=True)
class Settings:
    """Resolved runtime settings.

    Attributes:
        provider: Primary LLM provider, ``"groq"`` or ``"gemini"``.
        groq_api_key: Groq API key (may be empty in dry test contexts).
        groq_model: Groq model id.
        gemini_api_key: Gemini API key.
        gemini_model: Gemini model id.
        brand: Parsed brand kit dictionary.
        default_slides: Default slide count per carousel.
        root_dir / data_dir / output_dir / db_path / topic_bank_path: Paths.
    """

    provider: str
    groq_api_key: str
    groq_model: str
    gemini_api_key: str
    gemini_model: str
    brand: dict[str, Any]
    # Optional Pollinations token for registered (higher-quota) access; the
    # gradient fallback works fine without it, so this is never required.
    pollinations_token: str = ""
    # Phase 3: Telegram approval gate.
    telegram_bot_token: str = ""
    telegram_admin_chat_id: str = ""
    max_regenerations_per_day: int = 3
    # Phase 3: optional Supabase sync (feature-flagged on supabase_url).
    supabase_url: str = ""
    supabase_service_key: str = ""
    supabase_bucket: str = "slides"
    default_slides: int = 9
    root_dir: Path = ROOT_DIR
    data_dir: Path = DATA_DIR
    output_dir: Path = OUTPUT_DIR
    db_path: Path = DB_PATH
    topic_bank_path: Path = TOPIC_BANK_PATH
    assets_dir: Path = ASSETS_DIR
    fonts_dir: Path = FONTS_DIR

    @property
    def fallback_provider(self) -> str:
        """The provider used when the primary fails."""
        return "gemini" if self.provider == "groq" else "groq"


def _load_brand(path: Path = BRAND_PATH) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"Brand kit not found at {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:  # pragma: no cover - defensive
        raise ConfigError(f"Brand kit at {path} is not valid JSON: {exc}") from exc


def load_settings() -> Settings:
    """Build a :class:`Settings` from the environment and brand file.

    Does not require API keys to be present (so unit tests and ``--help`` work);
    the LLM layer validates keys lazily when a real call is attempted.
    """
    provider = os.getenv("GELIO_PROVIDER", "groq").strip().lower()
    if provider not in {"groq", "gemini"}:
        raise ConfigError(
            f"GELIO_PROVIDER must be 'groq' or 'gemini', got {provider!r}"
        )

    brand = _load_brand()

    return Settings(
        provider=provider,
        groq_api_key=os.getenv("GROQ_API_KEY", "").strip(),
        groq_model=os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile").strip(),
        gemini_api_key=os.getenv("GEMINI_API_KEY", "").strip(),
        gemini_model=os.getenv("GEMINI_MODEL", "gemini-2.0-flash").strip(),
        brand=brand,
        pollinations_token=os.getenv("POLLINATIONS_TOKEN", "").strip(),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        telegram_admin_chat_id=os.getenv("TELEGRAM_ADMIN_CHAT_ID", "").strip(),
        supabase_url=os.getenv("SUPABASE_URL", "").strip().rstrip("/"),
        supabase_service_key=os.getenv("SUPABASE_SERVICE_KEY", "").strip(),
        supabase_bucket=os.getenv("SUPABASE_BUCKET", "slides").strip(),
    )
