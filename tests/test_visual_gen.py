"""visual_gen tests: mocked success, gradient fallback, seed determinism."""

from __future__ import annotations

import io

import pytest
from PIL import Image

from gelio.visual_gen import (
    BackgroundResult,
    GradientFallback,
    ImageProvider,
    VisualGenerator,
    slide_seed,
)
from tests.conftest import BRAND

W, H = 1080, 1350


def _png_bytes(w=W, h=H, color=(10, 20, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="PNG")
    return buf.getvalue()


class _OKProvider(ImageProvider):
    name = "pollinations"

    def fetch(self, prompt, width, height, seed):
        return _png_bytes(width, height)


class _FailingProvider(ImageProvider):
    name = "pollinations"

    def __init__(self):
        self.calls = 0

    def fetch(self, prompt, width, height, seed):
        self.calls += 1
        raise RuntimeError("simulated API down")


def _fallback() -> GradientFallback:
    v = BRAND["visual"]
    return GradientFallback(v["primary_color"], v["background_tint"], v["accent_color"])


def test_seed_is_deterministic_and_index_sensitive():
    a = slide_seed("2026-06-11-decision-fatigue", 3)
    assert a == slide_seed("2026-06-11-decision-fatigue", 3)
    assert a != slide_seed("2026-06-11-decision-fatigue", 4)
    assert 0 <= a < 2**31


def test_returns_api_image_on_success():
    gen = VisualGenerator(primary=_OKProvider(), fallback=_fallback())
    result = gen.generate("a clean sky", W, H, seed=42)
    assert isinstance(result, BackgroundResult)
    assert result.source == "pollinations"
    img = Image.open(io.BytesIO(result.data))
    assert img.size == (W, H)


def test_falls_back_to_gradient_on_repeated_failure():
    gen = VisualGenerator(primary=_FailingProvider(), fallback=_fallback())
    result = gen.generate("a clean sky", W, H, seed=99)
    assert result.source == "fallback"
    img = Image.open(io.BytesIO(result.data))
    assert img.size == (W, H)
    assert img.mode == "RGB"


def test_no_primary_uses_fallback():
    gen = VisualGenerator(primary=None, fallback=_fallback())
    result = gen.generate("x", W, H, seed=1)
    assert result.source == "fallback"


def test_gradient_seed_changes_output():
    fb = _fallback()
    a = fb.render(W, H, seed=1)
    b = fb.render(W, H, seed=987654)
    assert a != b  # accent glow position varies with seed
