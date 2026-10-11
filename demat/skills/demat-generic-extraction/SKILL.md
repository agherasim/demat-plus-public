---
name: demat-generic-extraction
description: Fallback extraction skill used when triage cannot identify the DEMAT version. Transcribes whatever header fields and subtest rows are printed, in printed order, without a version-specific subtest table.
---

# DEMAT generic extraction (fallback)

Read the page image at the path given in the prompt. Transcribe it.

This skill is used when the triage pass could not determine which DEMAT
version the sheet is (`test_version` was `null`, or the detected version has
no dedicated skill yet). There is no version-specific subtest table here:
transcribe every subtest row actually printed on the sheet, in printed
order, using the abbreviation and name exactly as they appear.

Read `reference.md` in this directory before transcribing — it has the
shared transcription rules and the exact output JSON shape.

## Output contract

Write exactly one JSON object (the `ExtractedSheet` shape from
`reference.md`) to the `output_path` given in the prompt. Set `test_version`
to whatever you can read directly off the sheet (or `null` if genuinely not
determinable — this does not have to match the triage result).
