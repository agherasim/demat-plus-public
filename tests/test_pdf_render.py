"""`pdf_render.render_page` renders exactly the file `render_pages` would for
the same index (ADR 0016 re-extract reuses the page key/path)."""

from __future__ import annotations

import hashlib
import io
from pathlib import Path

import pypdfium2
import pytest

from demat import config, pdf_render


def _blank_pdf_bytes(n_pages: int) -> bytes:
    pdf = pypdfium2.PdfDocument.new()
    try:
        for i in range(n_pages):
            # Different sizes so the pages are distinguishable.
            pdf.new_page(200 + 50 * i, 300)
        buf = io.BytesIO()
        pdf.save(buf)
        return buf.getvalue()
    finally:
        pdf.close()


@pytest.fixture
def pages_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "PAGES_DIR", tmp_path / "pages")
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(config, "UPLOADS_DIR", tmp_path / "uploads")
    return tmp_path / "pages"


def test_render_page_matches_render_pages_per_index(pages_dir):
    pdf_bytes = _blank_pdf_bytes(3)
    sha = hashlib.sha256(pdf_bytes).hexdigest()

    all_pages = pdf_render.render_pages(pdf_bytes, sha)
    expected = {p.page_index: (p, Path(p.path).read_bytes()) for p in all_pages}
    for p in all_pages:
        Path(p.path).unlink()

    for index in range(3):
        single = pdf_render.render_page(pdf_bytes, sha, index)
        page, content = expected[index]
        assert single == page
        assert single.page_key == pdf_render._page_key(sha, index)
        assert Path(single.path) == pages_dir / f"{single.page_key}.png"
        assert Path(single.path).read_bytes() == content


@pytest.mark.parametrize("index", [-1, 2, 99])
def test_render_page_out_of_range_raises_index_error(pages_dir, index):
    pdf_bytes = _blank_pdf_bytes(2)
    sha = hashlib.sha256(pdf_bytes).hexdigest()
    with pytest.raises(IndexError):
        pdf_render.render_page(pdf_bytes, sha, index)
