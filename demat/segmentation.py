"""Page -> sheet segmentation seam. Default: one sheet per page.

Kept as its own module so the 1:1 page/sheet assumption is never baked into
callers (spec: "Multi-page-per-child sheets beyond leaving the segmentation
seam in place" is out of scope for now).
"""

from __future__ import annotations

from dataclasses import dataclass

from demat.pdf_render import PageImage


@dataclass(frozen=True)
class Sheet:
    page_indices: list[int]


def segment_pages(pages: list[PageImage]) -> list[Sheet]:
    return [Sheet(page_indices=[p.page_index]) for p in pages]
