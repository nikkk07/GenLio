"""Asset bootstrap: brand fonts (Montserrat/Inter) + Playwright Chromium.

Fonts are fetched from the Google Fonts GitHub mirror (variable TTFs, SIL Open
Font License); the templates embed them via @font-face so Chromium renders
deterministically offline. Binaries are NOT committed — ``assets/fonts/`` is
git-ignored and populated on demand via ``python run.py setup-assets``.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

logger = logging.getLogger("gelio.assets")

# name -> (filename, url). Variable fonts cover all weights via CSS font-weight;
# verified reachable (HTTP 200) at build time.
FONTS: dict[str, tuple[str, str]] = {
    "headline": (
        "Montserrat-Variable.ttf",
        "https://github.com/google/fonts/raw/main/ofl/montserrat/Montserrat%5Bwght%5D.ttf",
    ),
    "body": (
        "Inter-Variable.ttf",
        "https://github.com/google/fonts/raw/main/ofl/inter/Inter%5Bopsz%2Cwght%5D.ttf",
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


def install_chromium(*, with_deps: bool = False) -> None:
    """Install the Playwright Chromium browser used by the compositor.

    On Linux/CI add system libs with ``with_deps=True`` (``playwright install
    --with-deps chromium``).
    """
    cmd = [sys.executable, "-m", "playwright", "install"]
    if with_deps:
        cmd.append("--with-deps")
    cmd.append("chromium")
    logger.info("installing playwright chromium: %s", " ".join(cmd))
    try:
        subprocess.run(cmd, check=True)
    except FileNotFoundError as exc:  # playwright not installed
        raise AssetError(
            "playwright is not installed. `pip install -r requirements.txt` first."
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise AssetError(f"`playwright install chromium` failed: {exc}") from exc


def setup_assets(fonts_dir: Path, *, force: bool = False, with_deps: bool = False) -> list[Path]:
    """Download brand fonts and install Chromium. Returns the font paths."""
    paths = setup_fonts(fonts_dir, force=force)
    install_chromium(with_deps=with_deps)
    return paths
