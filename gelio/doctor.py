"""Preflight health check for unattended (CI) runs.

``python run.py doctor`` validates that everything an unattended daily run needs
is actually present *before* the pipeline spends time generating — and fails
loudly (non-zero exit) when a required piece is missing, so a broken GitHub
Actions run stops at the first step instead of half-publishing.

Checks are graded:
  * ``FAIL`` — the run cannot succeed (missing brand kit, logo, LLM key,
    Telegram credentials, or Supabase config when ``GELIO_STATE=supabase``).
  * ``WARN`` — degraded but survivable (no AI image provider → gradient
    fallback; fonts not yet fetched → scalable fallback font).
  * ``OK`` — good to go.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from config.settings import Settings

logger = logging.getLogger("gelio.doctor")

OK, WARN, FAIL = "OK", "WARN", "FAIL"
_ICON = {OK: "✅", WARN: "⚠️ ", FAIL: "❌"}


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""


def _brand(settings: Settings) -> Check:
    name = settings.brand.get("name") if isinstance(settings.brand, dict) else None
    if name:
        return Check("brand kit", OK, f"loaded ({name})")
    return Check("brand kit", FAIL, "config/brand.json missing or malformed")


def _logo(settings: Settings) -> Check:
    visual = settings.brand.get("visual", {}) if isinstance(settings.brand, dict) else {}
    logo = settings.root_dir / visual.get("logo_path", "assets/logo.png")
    if logo.exists():
        return Check("brand logo", OK, str(logo.relative_to(settings.root_dir)))
    return Check("brand logo", FAIL, f"missing {logo} (commit assets/logo.png)")


def _fonts(settings: Settings) -> Check:
    fonts = list(settings.fonts_dir.glob("*.ttf")) if settings.fonts_dir.exists() else []
    if fonts:
        return Check("brand fonts", OK, f"{len(fonts)} font files")
    return Check("brand fonts", WARN, "none found — run `setup-assets` (fallback font used otherwise)")


def _llm(settings: Settings) -> Check:
    if settings.provider == "groq":
        key = settings.groq_api_key
    else:
        key = settings.gemini_api_key
    if key:
        return Check("LLM provider", OK, f"{settings.provider} key present")
    return Check("LLM provider", FAIL, f"{settings.provider} key missing (set the API key)")


def _images(settings: Settings) -> Check:
    if settings.cloudflare_account_id and settings.cloudflare_api_token:
        return Check("AI images", OK, "cloudflare configured")
    if settings.together_api_key:
        return Check("AI images", OK, "together configured")
    return Check("AI images", WARN, "no provider — slides fall back to gradient (DEGRADED)")


def _telegram(settings: Settings) -> Check:
    if not settings.telegram_bot_token:
        return Check("telegram", FAIL, "TELEGRAM_BOT_TOKEN missing (approval gate disabled)")
    if not settings.telegram_admin_chat_ids:
        return Check("telegram", FAIL, "TELEGRAM_ADMIN_CHAT_ID missing")
    return Check("telegram", OK, f"{len(settings.telegram_admin_chat_ids)} admin(s)")


def _state(settings: Settings, *, probe: bool = True) -> Check:
    backend = settings.state_backend
    if backend == "sqlite":
        return Check("state backend", OK, "sqlite (local)")
    if backend != "supabase":
        return Check("state backend", FAIL, f"unknown GELIO_STATE={backend!r}")
    if not settings.supabase_url or not settings.supabase_service_key:
        return Check("state backend", FAIL, "supabase selected but SUPABASE_URL/SERVICE_KEY missing")
    if not probe:
        return Check("state backend", OK, "supabase configured")
    try:
        with httpx.Client(timeout=httpx.Timeout(10.0, connect=5.0)) as client:
            resp = client.get(
                f"{settings.supabase_url}/rest/v1/posts",
                params={"select": "id", "limit": "1"},
                headers={
                    "apikey": settings.supabase_service_key,
                    "Authorization": f"Bearer {settings.supabase_service_key}",
                },
            )
            resp.raise_for_status()
        return Check("state backend", OK, "supabase reachable")
    except Exception as exc:  # noqa: BLE001
        return Check("state backend", WARN, f"supabase configured but probe failed: {exc}")


def run_checks(settings: Settings, *, probe: bool = True) -> list[Check]:
    """Run every preflight check and return the graded results."""
    return [
        _brand(settings),
        _logo(settings),
        _fonts(settings),
        _llm(settings),
        _images(settings),
        _telegram(settings),
        _state(settings, probe=probe),
    ]


def format_report(checks: list[Check]) -> str:
    lines = [f"{_ICON[c.status]} {c.name:<14} {c.detail}" for c in checks]
    return "\n".join(lines)


def doctor(settings: Settings, *, probe: bool = True) -> int:
    """Print the health report; return 1 if any check FAILs, else 0."""
    checks = run_checks(settings, probe=probe)
    print("gelio doctor — preflight health check\n")
    print(format_report(checks))
    failed = [c for c in checks if c.status == FAIL]
    warned = [c for c in checks if c.status == WARN]
    print()
    if failed:
        print(f"❌ {len(failed)} check(s) FAILED — fix before running unattended.")
        return 1
    if warned:
        print(f"⚠️  {len(warned)} warning(s) — run may be DEGRADED but will proceed.")
    print("✅ doctor passed.")
    return 0
