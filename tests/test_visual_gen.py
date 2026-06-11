"""visual_gen tests: provider chain (cloudflare->together->gradient), b64, seeds."""

from __future__ import annotations

import base64
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


def _png_bytes(w=64, h=80, color=(10, 20, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="PNG")
    return buf.getvalue()


class _OK(ImageProvider):
    def __init__(self, name):
        self.name = name
        self.calls = 0

    def fetch(self, prompt, width, height, seed):
        self.calls += 1
        return _png_bytes()


class _Fail(ImageProvider):
    def __init__(self, name):
        self.name = name
        self.calls = 0

    def fetch(self, prompt, width, height, seed):
        self.calls += 1
        raise RuntimeError(f"{self.name} down")


def _gradient() -> GradientFallback:
    v = BRAND["visual"]
    return GradientFallback(v["blue"], v["navy"], v["gold"])


def test_seed_is_deterministic_and_index_sensitive():
    a = slide_seed("2026-06-11-myth", 3)
    assert a == slide_seed("2026-06-11-myth", 3)
    assert a != slide_seed("2026-06-11-myth", 4)
    assert 0 <= a < 2**31


def test_cloudflare_success_first_in_chain():
    cf, tg = _OK("cloudflare"), _OK("together")
    gen = VisualGenerator([cf, tg], _gradient())
    res = gen.generate("a sky", W, H, seed=1)
    assert isinstance(res, BackgroundResult)
    assert res.source == "cloudflare"
    assert tg.calls == 0  # short-circuits on first success
    assert Image.open(io.BytesIO(res.data)).size == (W, H)


def test_failover_cloudflare_to_together():
    cf, tg = _Fail("cloudflare"), _OK("together")
    gen = VisualGenerator([cf, tg], _gradient())
    res = gen.generate("a sky", W, H, seed=2)
    assert res.source == "together"
    assert cf.calls == 1 and tg.calls == 1


def test_failover_all_to_gradient():
    gen = VisualGenerator([_Fail("cloudflare"), _Fail("together")], _gradient())
    res = gen.generate("a sky", W, H, seed=3)
    assert res.source == "gradient"
    assert Image.open(io.BytesIO(res.data)).size == (W, H)


def test_no_providers_uses_gradient():
    res = VisualGenerator([], _gradient()).generate("x", W, H, seed=4)
    assert res.source == "gradient"


def test_cloudflare_decodes_base64(monkeypatch):
    from gelio import visual_gen as vg

    raw_png = _png_bytes()
    b64 = base64.b64encode(raw_png).decode()

    class FakeResp:
        status_code = 200
        headers = {"content-type": "application/json"}
        text = ""

        def json(self):
            return {"result": {"image": b64}}

    class FakeClient:
        def __init__(self, *a, **k): ...
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def post(self, *a, **k):
            return FakeResp()

    monkeypatch.setattr(vg.httpx, "Client", FakeClient)
    prov = vg.CloudflareProvider("acct", "token", "@cf/model")
    out = prov.fetch("p", W, H, 7)
    assert out == raw_png  # decoded back to the original PNG bytes


def test_gradient_seed_changes_output():
    fb = _gradient()
    assert fb.render(W, H, 1) != fb.render(W, H, 999999)
