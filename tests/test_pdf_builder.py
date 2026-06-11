"""pdf_builder tests: page count equals slide count."""

from __future__ import annotations

import pytest
from PIL import Image
from pypdf import PdfReader

from gelio.pdf_builder import PDFBuilderError, build_pdf


def _make_slides(tmp_path, n):
    paths = []
    for i in range(1, n + 1):
        p = tmp_path / f"slide_{i}.png"
        Image.new("RGB", (1080, 1350), (i * 10 % 255, 30, 60)).save(p)
        paths.append(p)
    return paths


def test_pdf_page_count_equals_slide_count(tmp_path):
    paths = _make_slides(tmp_path, 9)
    out = tmp_path / "carousel.pdf"
    build_pdf(paths, out)
    assert out.exists()
    reader = PdfReader(str(out))
    assert len(reader.pages) == 9


def test_pdf_single_slide(tmp_path):
    paths = _make_slides(tmp_path, 1)
    out = tmp_path / "carousel.pdf"
    build_pdf(paths, out)
    assert len(PdfReader(str(out)).pages) == 1


def test_empty_raises(tmp_path):
    with pytest.raises(PDFBuilderError):
        build_pdf([], tmp_path / "carousel.pdf")
