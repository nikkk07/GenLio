"""AI background image generation: Cloudflare -> Together -> gradient fallback.

The image model renders ONLY photographic backgrounds (never text — the HTML/CSS
compositor owns all typography). This module mirrors the resilience pattern of
:mod:`gelio.llm`: a provider abstraction, a feature-flagged chain tried in order,
and a guaranteed local fallback so a run NEVER dies if every API is down.

Providers (each enabled only when its env keys are present):
  1. Cloudflare Workers AI — ``@cf/black-forest-labs/flux-1-schnell`` (base64 JSON).
  2. Together AI — ``black-forest-labs/FLUX.1-schnell-Free`` (b64_json).
  3. GradientFallback — branded gradient, always succeeds (``source="gradient"``).

Each slide uses a deterministic seed (:func:`slide_seed`) so re-renders are stable.
"""

from __future__ import annotations

import base64
import hashlib
import io
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass

import httpx
from PIL import Image, ImageDraw, ImageOps
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

logger = logging.getLogger("gelio.visual")

_HTTP_TIMEOUT = httpx.Timeout(120.0, connect=10.0)


class ImageGenError(RuntimeError):
    """A non-retryable image generation error (fails over to the next provider)."""


class RetryableImageError(RuntimeError):
    """A transient image error (429 / 5xx) worth retrying."""


def slide_seed(post_id: str, index: int) -> int:
    """Deterministic 31-bit seed for a given post id + slide index."""
    digest = hashlib.sha256(f"{post_id}-{index}".encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % (2**31)


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return tuple(int(value[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


@dataclass
class BackgroundResult:
    """A generated background plus which provider produced it."""

    data: bytes  # PNG bytes, cover-fitted to the slide size
    source: str  # "cloudflare" | "together" | "gradient"
    seed: int


# Retry transient transport errors and classified 429/5xx.
_retry = retry(
    retry=retry_if_exception_type((httpx.TransportError, RetryableImageError)),
    wait=wait_exponential(multiplier=1, min=2, max=20),
    stop=stop_after_attempt(3),
    reraise=True,
)


def _classify(provider: str, resp: httpx.Response) -> None:
    """Raise the right error class for an HTTP failure."""
    if resp.status_code < 400:
        return
    body = resp.text[:200]
    msg = f"{provider} HTTP {resp.status_code}: {body}"
    if resp.status_code == 429 or resp.status_code >= 500:
        raise RetryableImageError(msg)
    raise ImageGenError(msg)


class ImageProvider(ABC):
    """Abstract background image provider."""

    name = "abstract"

    @abstractmethod
    def fetch(self, prompt: str, width: int, height: int, seed: int) -> bytes:
        """Return raw image bytes for ``prompt`` or raise on failure."""


class CloudflareProvider(ImageProvider):
    """Cloudflare Workers AI FLUX.1 [schnell] — free tier (~10 imgs/day)."""

    name = "cloudflare"

    def __init__(self, account_id: str, api_token: str, model: str) -> None:
        if not (account_id and api_token):
            raise ImageGenError("Cloudflare account id / api token not set")
        self._url = (
            f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/{model}"
        )
        self._token = api_token

    @_retry
    def fetch(self, prompt: str, width: int, height: int, seed: int) -> bytes:
        payload = {"prompt": prompt, "seed": seed, "steps": 4}
        headers = {"Authorization": f"Bearer {self._token}"}
        with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
            resp = client.post(self._url, json=payload, headers=headers)
        _classify(self.name, resp)
        ctype = resp.headers.get("content-type", "")
        # flux-1-schnell returns JSON {"result": {"image": "<base64>"}}, but some
        # Workers AI image models stream raw bytes — handle both.
        if ctype.startswith("image"):
            return resp.content
        data = resp.json()
        b64 = (data.get("result") or {}).get("image")
        if not b64:
            raise ImageGenError(f"cloudflare: no image in response: {str(data)[:200]}")
        return base64.b64decode(b64)


class TogetherProvider(ImageProvider):
    """Together AI FLUX.1 [schnell] free endpoint."""

    name = "together"
    _URL = "https://api.together.xyz/v1/images/generations"

    def __init__(self, api_key: str, model: str) -> None:
        if not api_key:
            raise ImageGenError("Together api key not set")
        self._key = api_key
        self._model = model

    @_retry
    def fetch(self, prompt: str, width: int, height: int, seed: int) -> bytes:
        payload = {
            "model": self._model,
            "prompt": prompt,
            "width": width,
            "height": height,
            "seed": seed,
            "n": 1,
            "steps": 4,
            "response_format": "b64_json",
        }
        headers = {"Authorization": f"Bearer {self._key}"}
        with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
            resp = client.post(self._URL, json=payload, headers=headers)
        _classify(self.name, resp)
        data = resp.json()
        try:
            item = data["data"][0]
        except (KeyError, IndexError) as exc:
            raise ImageGenError(f"together: bad response {str(data)[:200]}") from exc
        if item.get("b64_json"):
            return base64.b64decode(item["b64_json"])
        if item.get("url"):  # some responses return a URL instead of inline b64
            with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
                img = client.get(item["url"])
            _classify(self.name, img)
            return img.content
        raise ImageGenError(f"together: no image in item {str(item)[:200]}")


class GradientFallback:
    """Locally generated branded gradient — never fails."""

    name = "gradient"

    def __init__(self, primary: str, tint: str, accent: str) -> None:
        self._primary = _hex_to_rgb(primary)
        self._tint = _hex_to_rgb(tint)
        self._accent = _hex_to_rgb(accent)

    def render(self, width: int, height: int, seed: int) -> bytes:
        """A vertical brand gradient with a subtle, seed-placed accent glow."""
        top, bottom = self._primary, self._tint
        column = Image.new("RGB", (1, height))
        cpx = column.load()
        for y in range(height):
            t = y / max(height - 1, 1)
            cpx[0, y] = (
                int(top[0] + (bottom[0] - top[0]) * t),
                int(top[1] + (bottom[1] - top[1]) * t),
                int(top[2] + (bottom[2] - top[2]) * t),
            )
        base = column.resize((width, height))

        glow = Image.new("L", (width, height), 0)
        gd = ImageDraw.Draw(glow)
        cx = int((seed % 1000) / 1000 * width)
        cy = int((seed // 1000 % 1000) / 1000 * height)
        radius = int(min(width, height) * 0.45)
        gd.ellipse([cx - radius, cy - radius, cx + radius, cy + radius], fill=70)
        glow = glow.resize((width, height))
        accent_layer = Image.new("RGB", (width, height), self._accent)
        base = Image.composite(accent_layer, base, glow.point(lambda v: v // 2))

        buf = io.BytesIO()
        base.save(buf, format="PNG")
        return buf.getvalue()


class VisualGenerator:
    """Provider-chain background generator returning cover-fitted PNG bytes."""

    def __init__(
        self, providers: list[ImageProvider], gradient: GradientFallback
    ) -> None:
        self._providers = providers
        self._gradient = gradient

    def generate(
        self, prompt: str, width: int, height: int, seed: int
    ) -> BackgroundResult:
        for provider in self._providers:
            try:
                logger.info("image fetch provider=%s seed=%d", provider.name, seed)
                raw = provider.fetch(prompt, width, height, seed)
                png = _to_png_bytes(raw, width, height)
                return BackgroundResult(data=png, source=provider.name, seed=seed)
            except Exception as exc:  # noqa: BLE001 - any failure -> next provider
                logger.warning(
                    "image provider=%s failed, trying next: %s", provider.name, exc
                )
        logger.info("image source=gradient seed=%d", seed)
        png = self._gradient.render(width, height, seed)
        return BackgroundResult(data=png, source="gradient", seed=seed)


def _to_png_bytes(raw: bytes, width: int, height: int) -> bytes:
    """Normalize arbitrary image bytes to a cover-fitted RGB PNG."""
    img = Image.open(io.BytesIO(raw)).convert("RGB")
    img = ImageOps.fit(img, (width, height), method=Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def build_generator(settings) -> VisualGenerator:
    """Construct a VisualGenerator from settings + brand visual config.

    Providers are appended only when their keys are present, so the chain
    degrades gracefully to the gradient when nothing is configured.
    """
    visual = settings.brand.get("visual", {})
    gradient = GradientFallback(
        primary=visual.get("blue", visual.get("primary_color", "#0B3D91")),
        tint=visual.get("navy", visual.get("background_tint", "#0A1F3D")),
        accent=visual.get("gold", visual.get("accent_color", "#E8B33D")),
    )

    providers: list[ImageProvider] = []
    if settings.cloudflare_account_id and settings.cloudflare_api_token:
        providers.append(
            CloudflareProvider(
                settings.cloudflare_account_id,
                settings.cloudflare_api_token,
                settings.cloudflare_image_model,
            )
        )
    if settings.together_api_key:
        providers.append(
            TogetherProvider(settings.together_api_key, settings.together_image_model)
        )
    if not providers:
        logger.warning(
            "no AI image provider configured; slides will use the gradient fallback"
        )
    return VisualGenerator(providers=providers, gradient=gradient)
