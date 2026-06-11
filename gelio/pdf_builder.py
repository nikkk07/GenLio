"""Combine composited slide PNGs into a single LinkedIn-ready carousel PDF.

Built with Pillow's native multipage ``save_all`` to keep the stack light (no
reportlab, no browser binaries). One PDF page per slide, in order.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PIL import Image

logger = logging.getLogger("gelio.pdf")


class PDFBuilderError(RuntimeError):
    """Raised when no slides are provided to build a PDF."""


def build_pdf(slide_paths: list[Path], output_path: Path) -> Path:
    """Write ``slide_paths`` (in order) as a multipage PDF at ``output_path``.

    Returns the output path. Raises :class:`PDFBuilderError` if the list is empty.
    """
    if not slide_paths:
        raise PDFBuilderError("cannot build a PDF with zero slides")

    images = [Image.open(p).convert("RGB") for p in slide_paths]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    first, rest = images[0], images[1:]
    first.save(
        output_path,
        format="PDF",
        save_all=True,
        append_images=rest,
        resolution=150.0,
    )
    logger.info("wrote pdf=%s pages=%d", output_path, len(images))
    return output_path
