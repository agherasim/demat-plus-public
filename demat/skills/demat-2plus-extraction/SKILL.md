---
name: demat-2plus-extraction
description: Transcribe a DEMAT 2+ score sheet (header, child name, class, gender, test date, per-subtest RW/PR, the Gesamtwert block, and the free-text assessment note) into structured JSON.
---

# DEMAT 2+ extraction

Read the page image at the path given in the prompt. Transcribe it.

The prompt's "Expected output fields for this test type" section lists the
exact subtest keys (with their printed labels) and overall metric keys this
test declares — read that before transcribing. It is generated from
`demat/test_definitions/demat2plus.toml`, not hardcoded here, so it always
reflects the current definition.

Read `reference.md` in this directory before transcribing — it has the
shared transcription rules and the exact output JSON shape.

## Output contract

Write exactly one JSON object (the `ExtractedSheet` shape from
`reference.md`) to the `output_path` given in the prompt. `test_version`
should be `"DEMAT 2+"`. Include every subtest row actually printed on the
sheet, in printed order — even if it is not in the expected-fields list.
