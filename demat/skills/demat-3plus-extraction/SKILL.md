---
name: demat-3plus-extraction
description: Transcribe a DEMAT 3+ Testheft cover sheet (header, the 10 Aufgabentyp RW cells, the J/K/L/M Rohwert-Summen and the PR cells) into structured JSON.
---

# DEMAT 3+ extraction

Read the page image at the path given in the prompt. Transcribe it.

The prompt's "Expected output fields for this test type" section lists the
exact subtest keys (with their printed labels), the overall metric keys, and
the sum groups this test declares — read that before transcribing. It is
generated from `demat/test_definitions/demat3plus.toml`.

Read `reference.md` in this directory before transcribing — it has the
shared transcription rules and the exact output JSON shape.

## Layout (verified on two Testheft Form B cover sheets)

| Block | Printed content |
|---|---|
| Header | „DEMAT 3+“, „Testheft Form B“, Name, Klasse, Testdatum (Tag/Monat/Jahr boxes). **No Junge/Mädchen box.** |
| Aufgabentyp grid | 9 rows: ZS AD SU MU SR SZ FL LS LU. Each row has one RW cell with its printed max, placed in the column of its subtest: ZS/AD/SU/MU → column 1 (Arithmetik); SR (printed as **two** cells `SR1 (2)` and `SR2 (2)`) and LU → column 2 (Sachrechnen); SZ/FL/LS → column 3 (Geometrie). 10 RW cells in total. |
| Right-hand column | Fields `A`–`I`, one per Aufgabentyp row, each with a printed max: A(3) B(4) C(4) D(4) E(4) F(3) G(3) H(2) I(4). E is SR's combined field. |
| Rohwert-Summe | `J (15)` Arithmetik, `K (8)` Sachrechnen, `L (8)` Geometrie, `M (31)` Test-gesamtwert |
| Prozentrang | One numeric cell per column J–M |
| Prozentrang-Band | A 0–100 graph with one column per J–M |
| T-Wert | **None anywhere on the sheet** |

Printed maxima of the 10 Aufgabentyp cells: ZS 3, AD 4, SU 4, MU 4, SR1 2,
SR2 2, SZ 3, FL 3, LS 2, LU 4.

## Rules

1. **Always emit all 10 task rows**, in this printed order: ZS AD SU MU SR1
   SR2 SZ FL LS LU. Each row carries `rw`/`rw_max` as printed, `rw_state`,
   `pr: null` and `pr_state: "blank"` (these rows have no PR cell). If all of
   them are empty, still emit each one with `rw: null, rw_state: "blank"`.
2. **SR is two cells.** Emit SR1 and SR2 separately. Never emit a combined
   `SR` row.
3. **Always emit ARI, SAR and GEO**, from fields J/K/L:
   - `rw` only if a value is handwritten there, plus `rw_state`;
   - `pr` from the „Prozentrang“ cell under that column, plus `pr_state`;
   - `rw_max` 15 / 8 / 8.
4. **Always emit `rw_total`** (field M), `pr_total` (the Prozentrang cell
   under M) and `pr_band` (`value_state: "blank"` unless numbers are written
   beside the graph). **Do not emit `t_total` or `t_band`**: the sheet has no
   T cells.
5. **Never add anything up.** An empty J/K/L/M is `blank`, even when every
   task row is filled in. The app computes blank sums.
6. **Right-hand column A–I.** Do not emit it as rows. If an A–I cell is
   filled and disagrees with the grid cell (for E, compare with SR1+SR2),
   say so in `illegible_notes`.
7. **Gender.** There is no Junge/Mädchen box, so set `gender` to
   `"unknown"`.
8. **Date written next to the name.** If a date appears next to the name
   instead of in the Testdatum boxes, use it and say so in
   `illegible_notes`.

**Form A:** assume the same layout. If the printed layout differs, transcribe
what is printed and describe the difference in `illegible_notes`.

## Output contract

Write exactly one JSON object (the `ExtractedSheet` shape from
`reference.md`) to the `output_path` given in the prompt. `test_version`
should be `"DEMAT 3+"`.
