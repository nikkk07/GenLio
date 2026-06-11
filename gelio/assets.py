"""Asset bootstrap: download open-license brand fonts for the compositor.

Fetched from the Google Fonts GitHub mirror (raw static TTFs, both SIL Open Font
License). Binary fonts are NOT committed — ``assets/fonts/`` is git-ignored and
populated on demand via ``python run.py setup-assets``.
"""

from __future__ import annotations

import logging
from pathlib import Path

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

logger = logging.getLogger("gelio.assets")

# name -> (filename, url). Verified reachable (HTTP 200) at build time.
FONTS: dict[str, tuple[str, str]] = {
    "headline": (
        "Poppins-Bold.ttf",
        "https://github.com/google/fonts/raw/main/ofl/poppins/Poppins-Bold.ttf",
    ),
    "body": (
        "Lato-Regular.ttf",
        "https://github.com/google/fonts/raw/main/ofl/lato/Lato-Regular.ttf",
    ),
}


class AssetError(RuntimeError):
    """Raised when a required asset cannot be downloaded."""


@retry(
    retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
    wait=wait_exponential(multiplier=1, min=2, max=15),
    stop=stop_after_attempt(3),
    reraise=True,
)
def _download(url: str) -> bytes:
    with httpx.Client(timeout=60.0, follow_redirects=True) as client:
        resp = client.get(url)
        resp.raise_for_status()
        return resp.content


def setup_fonts(fonts_dir: Path, *, force: bool = False) -> list[Path]:
    """Download brand fonts into ``fonts_dir``; return the resulting paths.

    Skips files that already exist unless ``force`` is set.
    """
    fonts_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for role, (filename, url) in FONTS.items():
        dest = fonts_dir / filename
        if dest.exists() and not force:
            logger.info("font present, skipping role=%s file=%s", role, filename)
            written.append(dest)
            continue
        logger.info("downloading font role=%s url=%s", role, url)
        try:
            data = _download(url)
        except Exception as exc:  # noqa: BLE001
            raise AssetError(f"failed to download {filename} from {url}: {exc}") from exc
        if len(data) < 1000:
            raise AssetError(f"downloaded {filename} is suspiciously small ({len(data)} B)")
        dest.write_bytes(data)
        written.append(dest)
        logger.info("saved font %s (%d bytes)", dest, len(data))
    return written
