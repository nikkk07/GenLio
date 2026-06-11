"""Background image generation: Pollinations primary, gradient fallback.

The AI model renders ONLY backgrounds (never text — see compositor). This module
mirrors the resilience pattern of :mod:`gelio.llm`:

  * a provider abstraction (:class:`ImageProvider`),
  * the free, keyless Pollinations endpoint as primary, with tenacity backoff on
    transient failures (transport / 429 / 402 "queue full" / 5xx),
  * a locally-generated branded gradient as the guaranteed fallback.

A gelio run must never die because an image API is down, so
:class:`VisualGenerator` always returns valid PNG bytes — falling back to the
gradient and reporting ``source="fallback"`` when the API cannot be reached.
"""

from __future__ import annotations

import hashlib
import io
import logging
import urllib.parse
from abc import ABC, abstractmethod
from dataclasses import dataclass

import httpx
from PIL import Image, ImageDraw
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

logger = logging.getLogger("gelio.visual")

_HTTP_TIMEOUT = httpx.Timeout(120.0, connect=10.0)


class ImageGenError(RuntimeError):
    """A non-retryable image generation error."""


class RetryableImageError(RuntimeError):
    """A transient image error (402 queue-full / 429 / 5xx) worth retrying."""


def slide_seed(post_id: str, index: int) -> int:
    """Deterministic 31-bit seed for a given post id + slide index.

    Reproducible: the same artifact always requests the same background, so
    re-renders are stable.
    """
    digest = hashlib.sha256(f"{post_id}-{index}".encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % (2**31)


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return tuple(int(value[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


@dataclass
class BackgroundResult:
    """A generated background plus which path produced it."""

    data: bytes  # PNG bytes
    source: str  # "pollinations" | "fallback"
    seed: int


# Retry transient transport errors and classified 402/429/5xx.
_retry = retry(
    retry=retry_if_exception_type((httpx.TransportError, RetryableImageError)),
    wait=wait_exponential(multiplier=1, min=2, max=20),
    stop=stop_after_attempt(3),
    reraise=True,
)


class ImageProvider(ABC):
    """Abstract background image provider."""

    name = "abstract"

    @abstractmethod
    def fetch(self, prompt: str, width: int, height: int, seed: int) -> bytes:
        """Return PNG/JPEG image bytes for ``prompt`` or raise on failure."""


class PollinationsProvider(ImageProvider):
    """Free, keyless Pollinations image endpoint.

    Verified at build time: the anonymous tier rate-limits with HTTP 402
    ("queue full") and may require a registered token for higher quota. Both
    402 and 429 are treated as retryable; an optional token raises the quota.
    """

    name = "pollinations"
    _BASE = "https://image.pollinations.ai/prompt/"

    def __init__(self, token: str = "") -> None:
        self._token = token

    @_retry
    def fetch(self, prompt: str, width: int, height: int, seed: int) -> bytes:
        encoded = urllib.parse.quote(prompt, safe="")
        params = {
            "width": str(width),
            "height": str(height),
            "nologo": "true",
            "seed": str(seed),
        }
        if self._token:
            params["token"] = self._token
        url = f"{self._BASE}{encoded}?{urllib.parse.urlencode(params)}"

        with httpx.Client(timeout=_HTTP_TIMEOUT, follow_redirects=True) as client:
            resp = client.get(url, headers={"Referer": "https://gelio.local"})

        ctype = resp.headers.get("content-type", "")
        if resp.status_code >= 400:
            body = resp.text[:200]
            msg = f"pollinations HTTP {resp.status_code}: {body}"
            if resp.status_code in (402, 429) or resp.status_code >= 500:
                raise RetryableImageError(msg)
            raise ImageGenError(msg)
        if not ctype.startswith("image"):
            raise RetryableImageError(
                f"pollinations returned non-image content-type {ctype!r}"
            )
        return resp.content


class GradientFallback:
    """Locally generated branded diagonal gradient — never fails."""

    name = "fallback"

    def __init__(self, primary: str, tint: str, accent: str) -> None:
        self._primary = _hex_to_rgb(primary)
        self._tint = _hex_to_rgb(tint)
        self._accent = _hex_to_rgb(accent)

    def render(self, width: int, height: int, seed: int) -> bytes:
        """A vertical brand gradient with a subtle accent corner glow."""
        # Build a 1px-wide vertical gradient then stretch — O(height), not O(area).
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

        # Accent glow whose position varies deterministically with the seed,
        # so fallback slides are not identical but stay on-brand.
        glow = Image.new("L", (width, height), 0)
        gd = ImageDraw.Draw(glow)
        cx = int((seed % 1000) / 1000 * width)
        cy = int((seed // 1000 % 1000) / 1000 * height)
        radius = int(min(width, height) * 0.45)
        gd.ellipse(
            [cx - radius, cy - radius, cx + radius, cy + radius], fill=70
        )
        glow = glow.resize((width, height))
        accent_layer = Image.new("RGB", (width, height), self._accent)
        base = Image.composite(accent_layer, base, glow.point(lambda v: v // 2))

        buf = io.BytesIO()
        base.save(buf, format="PNG")
        return buf.getvalue()


class VisualGenerator:
    """Primary-with-fallback background generator returning PNG bytes."""

    def __init__(
        self,
        primary: ImageProvider | None,
        fallback: GradientFallback,
    ) -> None:
        self._primary = primary
        self._fallback = fallback

    def generate(
        self, prompt: str, width: int, height: int, seed: int
    ) -> BackgroundResult:
        if self._primary is not None:
            try:
                logger.info("image fetch provider=%s seed=%d", self._primary.name, seed)
                raw = self._primary.fetch(prompt, width, height, seed)
                png = _to_png_bytes(raw, width, height)
                return BackgroundResult(data=png, source=self._primary.name, seed=seed)
            except Exception as exc:  # noqa: BLE001 - any failure -> fallback
                logger.warning(
                    "image provider=%s failed, using gradient fallback: %s",
                    self._primary.name,
                    exc,
                )

        logger.info("image source=fallback seed=%d", seed)
        png = self._fallback.render(width, height, seed)
        return BackgroundResult(data=png, source="fallback", seed=seed)


def _to_png_bytes(raw: bytes, width: int, height: int) -> bytes:
    """Normalize arbitrary image bytes to a cover-fitted RGB PNG."""
    from PIL import ImageOps

    img = Image.open(io.BytesIO(raw)).convert("RGB")
    img = ImageOps.fit(img, (width, height), method=Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def build_generator(settings) -> VisualGenerator:
    """Construct a VisualGenerator from settings + brand visual config."""
    visual = settings.brand.get("visual", {})
    fallback = GradientFallback(
        primary=visual.get("primary_color", "#0B3D91"),
        tint=visual.get("background_tint", "#0A1F3D"),
        accent=visual.get("accent_color", "#F5A623"),
    )
    primary = PollinationsProvider(token=getattr(settings, "pollinations_token", ""))
    return VisualGenerator(primary=primary, fallback=fallback)
