"""PDF -> per-page PNG rendering (pypdfium2, license-clean; see ADR 0002 and
ADR 0009 §5, which replaced the previous PyMuPDF (AGPL) implementation).

Isolated so a rendering-engine swap stays a one-file change.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

import pypdfium2

from demat import config


@dataclass(frozen=True)
class PageImage:
    page_index: int
    page_key: str
    path: str


def _page_key(file_sha256: str, page_index: int) -> str:
    return hashlib.sha256(f"{file_sha256}:{page_index}".encode()).hexdigest()


def render_pages(
    pdf_bytes: bytes,
    file_sha256: str,
    dpi: int = config.DPI,
    max_edge_px: int = config.MAX_IMAGE_EDGE_PX,
) -> list[PageImage]:
    """Render every page of `pdf_bytes` to a PNG in `PAGES_DIR`.

    File name and dedup key (`page_key`) are derived from the *source page
    identity* (`file_sha256` + `page_index`), not from the rendered PNG, so
    changing `dpi`/`max_edge_px` never silently re-triggers extraction.
    """
    config.ensure_data_dirs()
    pages: list[PageImage] = []

    pdf = pypdfium2.PdfDocument(pdf_bytes)
    try:
        for page_index in range(len(pdf)):
            pages.append(_render_one(pdf, file_sha256, page_index, dpi, max_edge_px))
    finally:
        pdf.close()

    return pages


def render_page(
    pdf_bytes: bytes,
    file_sha256: str,
    page_index: int,
    dpi: int = config.DPI,
    max_edge_px: int = config.MAX_IMAGE_EDGE_PX,
) -> PageImage:
    """Render only page `page_index` of `pdf_bytes` (ADR 0016: re-extract one
    already-extracted page from the stored source PDF).

    Writes the exact same `PAGES_DIR/<page_key>.png` `render_pages` would
    for that index. Raises `IndexError` for an out-of-range `page_index`.
    """
    config.ensure_data_dirs()
    pdf = pypdfium2.PdfDocument(pdf_bytes)
    try:
        if not 0 <= page_index < len(pdf):
            raise IndexError(
                f"page_index {page_index} out of range for a {len(pdf)}-page PDF"
            )
        return _render_one(pdf, file_sha256, page_index, dpi, max_edge_px)
    finally:
        pdf.close()


def _render_one(
    pdf: pypdfium2.PdfDocument,
    file_sha256: str,
    page_index: int,
    dpi: int,
    max_edge_px: int,
) -> PageImage:
    base_scale = dpi / 72
    page = pdf[page_index]
    width_pt, height_pt = page.get_size()
    long_edge_px = max(width_pt, height_pt) * base_scale

    scale = base_scale
    if long_edge_px > max_edge_px:
        scale *= max_edge_px / long_edge_px

    bitmap = page.render(scale=scale)
    image = bitmap.to_pil()

    page_key = _page_key(file_sha256, page_index)
    out_path = config.PAGES_DIR / f"{page_key}.png"
    image.save(out_path)
    return PageImage(page_index=page_index, page_key=page_key, path=str(out_path))
