---
name: demat-1plus-extraction
description: Transcribe a DEMAT 1+ score sheet into structured JSON. STUB — no sample sheet has been analyzed yet; the expected subtest/metric list comes from the definition, not a verified sheet layout.
---

# DEMAT 1+ extraction (stub)

Read the page image at the path given in the prompt. Transcribe it.

**No DEMAT 1+ sample sheet has been analyzed.** The prompt's "Expected
output fields for this test type" section lists the subtest keys and
overall metric keys from `demat/test_definitions/demat1plus.toml`, sourced
from the external knowledge base rather than a real sheet — treat it as a
guide, not a verified layout. Transcribe whatever subtest rows are actually
printed on the sheet, in printed order, using the abbreviation and name
exactly as they appear. If a printed subtest doesn't match the expected
list, still include it and note it in `illegible_notes` per the shared
rules in `reference.md`.

Read `reference.md` in this directory before transcribing — it has the
shared transcription rules and the exact output JSON shape.

## Output contract

Write exactly one JSON object (the `ExtractedSheet` shape from
`reference.md`) to the `output_path` given in the prompt. `test_version`
should be `"DEMAT 1+"`.
