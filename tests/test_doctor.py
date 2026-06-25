"""Doctor preflight grading tests (no network; probe disabled)."""

from __future__ import annotations

from pathlib import Path

from config.settings import Settings
from gelio.doctor import FAIL, OK, WARN, run_checks
from tests.conftest import BRAND


def _settings(tmp_path: Path, **kw) -> Settings:
    base = dict(
        provider="groq", groq_api_key="g", groq_model="m", gemini_api_key="", gemini_model="m",
        brand=BRAND, telegram_bot_token="t", telegram_admin_chat_id="999",
        cloudflare_account_id="acc", cloudflare_api_token="tok",
        output_dir=tmp_path / "output", db_path=tmp_path / "db.sqlite",
        root_dir=tmp_path, assets_dir=tmp_path / "assets", fonts_dir=tmp_path / "assets" / "fonts",
    )
    base.update(kw)
    return Settings(**base)


def _by_name(checks):
    return {c.name: c for c in checks}


def _make_logo(tmp_path: Path):
    logo = tmp_path / "assets" / "logo.png"
    logo.parent.mkdir(parents=True, exist_ok=True)
    logo.write_bytes(b"PNG")
    return logo


def test_all_green_when_configured(tmp_path):
    _make_logo(tmp_path)
    checks = _by_name(run_checks(_settings(tmp_path), probe=False))
    assert checks["brand kit"].status == OK
    assert checks["brand logo"].status == OK
    assert checks["LLM provider"].status == OK
    assert checks["telegram"].status == OK
    assert checks["state backend"].status == OK


def test_missing_logo_fails(tmp_path):
    checks = _by_name(run_checks(_settings(tmp_path), probe=False))
    assert checks["brand logo"].status == FAIL


def test_missing_llm_key_fails(tmp_path):
    _make_logo(tmp_path)
    checks = _by_name(run_checks(_settings(tmp_path, groq_api_key=""), probe=False))
    assert checks["LLM provider"].status == FAIL


def test_missing_telegram_fails(tmp_path):
    _make_logo(tmp_path)
    checks = _by_name(run_checks(_settings(tmp_path, telegram_bot_token=""), probe=False))
    assert checks["telegram"].status == FAIL


def test_no_image_provider_warns(tmp_path):
    _make_logo(tmp_path)
    checks = _by_name(
        run_checks(_settings(tmp_path, cloudflare_account_id="", cloudflare_api_token=""), probe=False)
    )
    assert checks["AI images"].status == WARN


def test_supabase_backend_requires_keys(tmp_path):
    _make_logo(tmp_path)
    checks = _by_name(run_checks(_settings(tmp_path, state_backend="supabase"), probe=False))
    assert checks["state backend"].status == FAIL  # url/key missing

    checks = _by_name(
        run_checks(
            _settings(
                tmp_path, state_backend="supabase",
                supabase_url="https://x.supabase.co", supabase_service_key="svc",
            ),
            probe=False,
        )
    )
    assert checks["state backend"].status == OK
