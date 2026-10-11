# ADR 0002 — Extract DEMAT sheets by rendering PDF pages to images and reading them with a Claude vision agent

- Status: Accepted (agent/skill layer added by ADR 0004)
- Date: 2026-09-20

## Context

Uploaded DEMAT PDFs are scans/photos of handwritten Hogrefe score sheets (DEMAT 1+, 2+,
3+, 4+). There is no usable text layer, so extraction must be vision-based. Each PDF may
contain many pages, normally one child's sheet per page.

This ADR covers **how pages reach the model and how results are validated**. Which
framework issues the model call, and how DEMAT-version-specific instructions are packaged,
is decided separately in ADR 0004.

## Decision

**Rendering.** Render each PDF page to a PNG at 200 DPI with PyMuPDF (`pymupdf`),
downscaling so the long edge is at most 1568 px (the point beyond which the API
downsamples anyway). Page images are written to a local, gitignored `data/pages/`
directory, named by the page's SHA-256.

**Images are passed by file path, not only inline.** Because the extraction step runs
through an agent with filesystem tools (ADR 0004), the durable on-disk page image is the
interface: the agent is told to read `data/pages/<sha256>.png`. The same file also backs
the review UI and any re-run. A direct-API fallback path base64-encodes the same file.

**One agent run per sheet**, never one per PDF: isolated retry, per-sheet provenance,
per-sheet progress in the UI.

**Confidence.** The output schema requires per-field confidence (`high|medium|low`) for
the name, the date and every numeric cell, plus a free-text `illegible_notes`. Low
confidence anywhere marks the assessment `needs_review`.

**Deterministic validation on top of model confidence.** Arithmetic cross-checks run in
Python after parsing: per-subtest `RW <= RW_max`, `sum(subtest RW) == Gesamtwert RW`
(warning, not hard failure — not every DEMAT version guarantees this), `PR in 0..100`,
`T-Wert in 20..80`, date parses to a plausible school-year date. These catch
confidently-wrong transcriptions, which self-reported confidence structurally misses.

**Raw response retained.** The full JSON payload is stored on the assessment row so the
data model can be re-derived without re-billing.

## Alternatives rejected

- **Native `document` base64-PDF content blocks.** Works, and Claude does render pages
  internally — but it puts a whole multi-child PDF in one call: one failure retries
  everything, per-sheet provenance is muddier, there is no page image to show in a
  review UI, and there is no DPI control for faint handwriting.
- **`pdf2image`.** Depends on a poppler system binary that `uv add` cannot install;
  rejected as a portability hazard on a uv-managed project.
- **`pypdfium2` instead of PyMuPDF.** BSD/Apache-licensed pure wheel and a fine drop-in;
  PyMuPDF chosen for its simpler rasterization API and better scanned-PDF handling. Noted
  explicitly because PyMuPDF is AGPL — if this app is ever distributed rather than run
  locally, switch to `pypdfium2`. The renderer is isolated in one module to make that a
  one-file change.
- **Classic OCR (tesseract) with a rules layer.** Poor on handwritten German digits and
  checkbox marks; would need an LLM pass anyway.
