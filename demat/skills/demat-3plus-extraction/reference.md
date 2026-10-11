<!-- AUTHORED HERE. Copied verbatim into every skill directory by
     scripts/sync_skills.py. Do not edit the generated copies directly. -->

# Shared DEMAT transcription rules

These rules apply to every DEMAT extraction skill, on top of whatever
version-specific expected-field list is injected into the prompt at call
time (from that test's authored definition — see `demat/test_definitions/`)
or given in `SKILL.md`.

## Transcribe only what is written

- Never infer a PR (percentile rank) from an RW (raw score); never compute a
  total that is not printed on the sheet. If a cell is blank or missing,
  the field is `null` — never a guess.
- Report `"low"` confidence rather than guessing an illegible digit or letter,
  and name the uncertain field explicitly in `illegible_notes`.
- German text stays verbatim, including umlauts and abbreviations exactly as
  printed or handwritten (e.g. `LÄ`, not `LAE` or `LA`).
- Do not invent subtests: return the rows actually printed on the sheet, in
  printed order, even if a row is not in this skill's expected table. If a
  printed subtest is not one this skill expects, still include it and note it
  in `illegible_notes`.
- Checkbox `Junge` / `Mädchen` maps to `"junge"` / `"maedchen"`; if neither is
  checked (or both, or it's unclear), use `"unknown"`.
- **Raw scores may be half points.** Some subtests (e.g. DEMAT 2+
  Sachaufgaben) allow half-point raw scores. Transcribe exactly what is
  written, including the German decimal comma — write `3,5` as `"3,5"`, not
  `"3.5"` and not rounded to `"4"` or truncated to `"3"`.
- **Every score value is a string.** `rw`, `rw_max`, and every overall
  metric's `value` are transcribed as the literal printed text (e.g.
  `"20,5"`, `"8-16"`), never as a JSON number — this preserves what was
  printed and lets the app do the typed conversion. `pr` (percentile rank)
  stays a plain integer since it never has half-point steps.

## Confidence

Every name, date, and numeric field carries its own `confidence`
(`"high"` | `"medium"` | `"low"`). Use `"low"` for anything genuinely
illegible or ambiguous. Use `"medium"` for a legible-but-uncertain read (e.g.
a smudged digit you are fairly, not fully, confident about). `overall_confidence`
summarizes the sheet as a whole; it is not the minimum of the per-field
confidences, but if any field is `"low"` the overall confidence should
usually not be `"high"`.

## Norm cells left for the evaluator

On DEMAT sheets the teacher fills in the RWs. PR/PR-Band/T/T-Band are
usually looked up later from the manual, so they are **often empty. That is
normal and expected** — it is not a defect or a sign of a damaged sheet.

Set `pr_state` on every subtest row and `value_state` on every `overall`
entry:

- `"written"`: something is written. Transcribe it.
- `"blank"`: visibly empty. The value is `null`.
- `"illegible"`: written but unreadable. The value is `null`, confidence is
  `"low"`, and the cell is named in `illegible_notes`.

**Empty is not illegible.** An empty cell does not lower confidence and is
not mentioned in `illegible_notes`. Confidence describes only the
legibility of what is written, never whether a cell was filled in at all.

**Emit every expected metric.** Emit every expected `overall` metric that
has a cell on the sheet, even when empty (`value: null, value_state:
"blank"`). Omit a metric only if the sheet has no cell for it at all.

**`rw_state`.** Set `rw_state` on every subtest row too, with the same three
values and the same "empty is not illegible" rule as `pr_state`.

**Rows with no PR cell.** If a row has no PR cell on the sheet at all (for
example a task-type row), emit `pr: null, pr_state: "blank"`.

## Sum cells

Some sheets print a cell for the sum of other rows (a „Rohwert-Summe“).

- Transcribe a sum cell only if a value is handwritten in it.
- If it is empty, emit the row with `rw: null, rw_state: "blank"` — even when
  every row it sums is filled in.
- Never compute it: never add the rows up yourself. The app computes blank
  sums.

## Output contract

The prompt supplies an absolute `output_path`. Write **exactly one JSON
object** to that path and nothing else — any conversational text you produce
in the transcript is ignored; only the file contents are read. Do not wrap
the JSON in markdown fences inside the file.

### Triage output (`demat-sheet-triage` only)

```json
{
  "is_demat_sheet": true,
  "test_version": "DEMAT 2+",
  "test_form": "A",
  "confidence": "high",
  "notes": null
}
```

`test_version` is one of `"DEMAT 1+"`, `"DEMAT 2+"`, `"DEMAT 3+"`,
`"DEMAT 5+"`, or `null` if it cannot be determined. There is no DEMAT 4+.
`test_form` is the printed form letter (e.g. `"A"` from "Testheft Form A"),
or `null`.

### Extraction output (all `*-extraction` skills)

```json
{
  "is_demat_sheet": true,
  "test_version": "DEMAT 2+",
  "test_form": "A",
  "child_name": "Max Mustermann",
  "child_name_confidence": "high",
  "klasse": "3c",
  "gender": "junge",
  "test_date_raw": "16.09.26",
  "test_date_iso": "2026-09-16",
  "test_date_confidence": "high",
  "subtests": [
    {
      "key": "ZE",
      "name": "Zahleneigenschaften",
      "rw": "12",
      "rw_max": "15",
      "rw_state": "written",
      "pr": 63,
      "pr_state": "written",
      "confidence": "high"
    },
    {
      "key": "LA",
      "name": "Längenvergleich",
      "rw": "9",
      "rw_max": "15",
      "rw_state": "written",
      "pr": null,
      "pr_state": "blank",
      "confidence": "high"
    }
  ],
  "overall": [
    {"key": "rw_total", "value": "88", "value_state": "written", "confidence": "high"},
    {"key": "pr_total", "value": null, "value_state": "blank", "confidence": "high"},
    {"key": "pr_band", "value": null, "value_state": "blank", "confidence": "high"},
    {"key": "t_total", "value": null, "value_state": "blank", "confidence": "high"},
    {"key": "t_band", "value": null, "value_state": "blank", "confidence": "high"}
  ],
  "assessment_note": "Freitext, wie handschriftlich notiert.",
  "illegible_notes": null,
  "overall_confidence": "high"
}
```

- `test_date_raw` is exactly as written (e.g. `"16.09.26"`, `DD.MM.YY`).
  `test_date_iso` is the same date normalized to `YYYY-MM-DD` with a
  two-digit year assumed to be `20YY`, or `null` if it cannot be parsed.
- `subtests` preserves the order printed on the sheet. `key` is the
  abbreviation as printed on the sheet (e.g. `LÄ`) — the app resolves it to
  the canonical key it expects (e.g. `LA`); you do not need to know that
  mapping, just transcribe what is printed.
- `overall` is a list, not a fixed set of fields: emit one entry per metric
  the prompt's "Expected output fields for this test type" section asks
  for (e.g. `rw_total`, `pr_total`, `pr_band`, `t_total`, `t_band` for a
  DEMAT test), each as `{"key": ..., "value": ..., "confidence": ...}`. If
  that section is absent (generic fallback), emit whatever Gesamtwert-block
  fields are actually printed, using your best judgement for the key names.
- Every field that is unreadable on the sheet is `null`, with the
  corresponding `confidence` set to `"low"` and a mention in
  `illegible_notes` — never silently dropped. A visibly *blank* cell is also
  `null`, but is not a confidence problem: see "Norm cells left for the
  evaluator" above for `pr_state`/`value_state`, which distinguish the two.
  Omit a metric from `overall` only if the sheet has no cell for it at all.
