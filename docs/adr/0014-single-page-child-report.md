# ADR 0014 — Single-page child report (Einzelauswertung): deterministic, sheet-faithful, grayscale, stored rating

- Status: Proposed
- Date: 2026-09-27. Revised the same day after user review:
  - grayscale only
  - a sheet-faithful layout only
  - corrected the note on test version vs. class
  - the band is drawn as two connected dots
  - the added Gesamtwert row on 1+/2+/5+ is dropped, so the report stays strictly faithful to each
    sheet
  - every diagram shades the „durchschnittlich“ zone of its PR axis in white, where the KB gives
    that range on the PR scale:
    - 3+ corridor
    - 2+ and 5+ Profil PR
    - not 1+

    The tables are not affected; an intermediate table-highlight revision was withdrawn.
- Extends: ADR 0008 (report pipeline, artifact format, KB-from-Python rule) and ADR 0010 (asserted
  norm variant)
- Depends on, but does not require: ADR 0013 (per-cell provenance; used when present)
- Examples: `docs/adr/0014-child-report-examples.md` (three rendered samples with fictional data)

## Context

The Classroom Profile (ADR 0008) answers "what does this class look like". Teachers also need a
one-page sheet per child that they can print, file, or hand to parents. This sheet must:

- show the child's name, class, test date and overall rating
- show where the score sits in the manual's rating bands
- carry the pseudonymous `person_id` (ADR 0001) for internal traceability

ADR 0008's spec listed this "Einzelauswertung" as out of scope. This ADR brings it in.

Facts that shape the decision:

- **Three sets of real scanned sheets were reviewed.** Each filename gives the class the test was
  taken in.
  - A DEMAT version is normally given at the *start* of a school year to assess the *previous*
    grade's curriculum. That is why the class label reads one grade higher than the version number.
  - The manuals publish norms for exactly this timing (`k3`, `anfang_k4`).
  - `3b.pdf` is class 3B, DEMAT 2+ Form A.
    - A table with one row per subtest (10 rows): name, key, RW (max), PR („aus Handbuch“).
    - A „Profil PR“ column on the right, where the subtest PR points are plotted on a 0–100 grid
      and joined by lines.
    - Below the table, a Gesamtwert block of plain numbers: Rohwert (ges), Prozentrang,
      Prozentrang-Band, T-Wert and T-Wert-Band.
    - The overall value and its bands are not plotted anywhere on the sheet.
  - `2d-demat.pdf` is class 2d, DEMAT 1+ Form A.
    - The same layout, with 9 rows.
    - The Gesamtwert block has PR, T-Wert and T-Wert-Band only. DEMAT 1+ publishes no PR-Band.
  - `4ademat.pdf` is class 4A, DEMAT 3+ Form B.
    - Nine task-type RWs, rolled up into three subtest columns plus a Testgesamtwert column.
    - A „Rohwert-Summe“ row and a „Prozentrang“ row.
    - A „Prozentrang-Band“ diagram. For each column the upper and lower band bounds are marked, and
      the marks are joined by lines into a corridor.
  - No sheet's diagram marks an average zone. The KB describes the 3+ graphic as band bounds only
    (`demat3plus_diagnostik.md`, „Grafik auf dem Deckblatt“), and the 2+ profile as PR points only
    (`demat2plus_diagnostik.md` §5.2).
  - On every sheet the evaluator hand-writes the verbal rating under the table. That rating is the
    headline of the sheet.
  - The sheets use no colour. Their only graphic is the one diagram printed on the form.
- **The hand-written norm values come from the gender-specific tables, not `_gesamt`.** Checked
  against the KB:
  - A DEMAT 2+ sheet's hand-written PR, PR-Band, T and T-Band match the `k3_jungen` row for its
    RW exactly; the `k3_gesamt` row differs in every one of the four fields.
  - A DEMAT 1+ sheet's PR, T and T-Band match `k2_jungen` exactly and differ from `k2_gesamt`.
  - Both differences cross a band edge. **Re-deriving the rating from the RW via `_gesamt` would
    contradict the rating the evaluator wrote on the sheet.**
- **Band edges are not exposed by the KB CLI.** `lookup.py classify` classifies one value at a
  time, and no command lists the band edges. It classifies 1+ and 5+ on the T scale only.
- **The „durchschnittlich“ range per version.** All paths below are in `demat-knowledge-base/`. The
  diagrams use a PR axis, so what matters is whether the range is stated on the PR scale.

  | Version | „durchschnittlich“ | Stated on the PR scale? | Source |
  |---|---|---|---|
  | 3+ | PR 25–75 | yes | Tabelle 4, S. 25. `references/demat3plus_diagnostik.md:242–251`, `references/demat3plus.md:60`, `references/allgemein.md:33`, `scripts/lookup.py:35–36` |
  | 2+ | PR 26–75 | yes | S. 24. `references/demat2plus_diagnostik.md:311`, `references/demat2plus.md:69`, `allgemein.md:33`, `lookup.py:33–34` |
  | 5+ | T 40–60 = PR 15,8–84,5 | yes, in the manual's own Tabelle 3, but **not** through `lookup.py` | Tabelle 3, S. 21. `references/demat5plus_diagnostik.md:222`, `references/demat5plus.md:79`, `allgemein.md:33` |
  | 1+ | T 40–60 | **no**. The KB gives no PR equivalent anywhere | S. 25. `references/demat1plus_diagnostik.md:302`, `allgemein.md:33`, `lookup.py:31–32` |

  This is the manual's rating category. It is not the „grau unterlegt … mittlere 50 %“ shading
  that the manuals use in their norm tables, which is a different concept.
- **Subtest counts and classification basis differ by version.**

  | Version | Subtests | Basis |
  |---|---|---|
  | 1+ | 9 | T |
  | 2+ | 10 | PR |
  | 3+ | 3 | PR |
  | 5+ | 3 (B not normed) | T |

  DEMAT 3+ task types are not modelled. No DEMAT 5+ sheet has been seen.
- **No PDF renderer is installed.** The app already has a chrome-less print view (`?view=print`).
- **ADR 0013's provenance columns are not in `schema.sql` yet.**

The detailed implementation spec is `docs/specs/0014-child-report.md`. This ADR is the durable
record of the decisions.

## Decision

**A separate, fully deterministic pipeline that reuses the class-profile building blocks.**

- A child report is `assemble → norms → render`, with no LLM stage.
- Every sentence on the page is one of:
  - a number computed in Python
  - a label from `lookup.py`
  - static template text
- The per-child assembly code inside `build_class_data()` is extracted into a shared
  `build_child_data()`. Class-profile behaviour is unchanged.

**The final rating is `classify()` applied to the stored, human-validated overall value. It is
never re-derived from the RW.**

- The value is `pr_total` for 2+/3+ and `t_total` for 1+/5+, located through the definition's
  `norm_field`.
- A missing value renders „nicht bestimmbar“.
- A boundary value that falls in two categories shows both, with the manual's note.

**The report names the norm *variant*, not a `_gesamt` table, unless provenance proves it.**

- The header shows `norm_variants.label_for()`, with no gender suffix.
- The grade cross-check message is passed the variant key.
- A specific table name appears only in the footer provenance phrase, and only for cells ADR 0013
  marks `computed`.

**Strictly what the scanned sheet prints. No invented charts, rows or markers.** The one exception
is the user-requested average zone in the diagrams, described below.

1. **A results table laid out like the version's scanned sheet.**
   - DEMAT 1+/2+: one row per subtest, then the Gesamtwert block with the rows that version prints.
     The bands (PR-Band, T-Wert-Band) are plain numbers in grey cells, exactly as on the sheet.
   - DEMAT 3+: subtest columns plus a Testgesamtwert column, with rows for Rohwert-Summe,
     Prozentrang and Prozentrang-Band.
   - DEMAT 5+: provisionally the 1+/2+ layout, with the T-Wert-Band (90 %) as a number.
2. **Exactly the one diagram that sheet prints, in the same place.**
   - DEMAT 1+/2+/5+: the „Profil PR“ column. It plots the subtest PRs only. The overall value and
     its bands are not plotted, because the sheet does not plot them.
   - DEMAT 3+: the „Prozentrang-Band“ panel below the table.
3. **The manual's rating-band table as text**, with the child's category marked.

Earlier drafts had a separate range-axis diagram, a subtest bar profile, a colour chip, and an extra
„Gesamtwert“ band row under the 1+/2+/5+ profile. All four are dropped.

**In the DEMAT 3+ corridor, the band (a range) is drawn as two dots joined by a line, as on the
paper sheet.**

- The PR-Band is drawn as:
  - one dot at its lower edge and one dot at its upper edge (grey `#8C8C8C`, round, 7 px)
  - a grey line (2 px) connecting them
- This mirrors how the evaluator draws the corridor on the DEMAT 3+ sheet: points at the band
  bounds, joined by a line.
- The 3+ corridor is the only diagram that draws a band. On 1+/2+/5+ no band is drawn anywhere.
- The dots are `<rect>` elements with `rx`/`ry` equal to half their size, so the SVG stays inside
  ADR 0008's subset (`svg`, `rect`, `line`, `text`, `g`).

**Every diagram shades the „durchschnittlich“ zone of its PR axis white, where the KB states that
range on the PR scale.**

| Diagram | Zone | Orientation |
|---|---|---|
| DEMAT 3+ corridor | PR 25–75 | horizontal strip across all four columns |
| DEMAT 2+ Profil PR | PR 26–75 | vertical strip across all subtest rows |
| DEMAT 5+ Profil PR (provisional layout) | PR 15,8–84,5 | vertical strip |
| DEMAT 1+ Profil PR | **none** | — |

- **Where the edges come from.**
  - For 2+ and 3+, the edges come at runtime from the KB via `band_segments()` (the segment
    labelled „durchschnittlich“). They are never hardcoded, and a test pins them to the values
    above.
  - For 5+, `lookup.py` classifies only on T, so the PR edges cannot be probed. They are a single
    cited constant in `norms.py` (Tabelle 3, S. 21; `demat5plus_diagnostik.md:222`). **This is the
    one deliberate exception to "band edges only from the KB CLI".** An upstream `lookup.py`
    command that exposes the PR equivalents would remove it.
- **Known asymmetry: DEMAT 1+ has no zone.** The KB states the 1+ average range only as T 40–60 and
  gives no PR equivalent. The Profil PR is a PR axis, and the design does not estimate a conversion.
  The 1+ Profil PR therefore stays unshaded until the KB provides a PR-scale range for 1+.
- **Draw order, the same in every diagram:**
  1. grey background
  2. white zone
  3. grid lines
  4. band dots and profile line
  5. value squares
  6. text
- The zone is a property of the scale. It sits in the same place for every child of the version,
  never depends on the child's result, and is drawn even when there is no overall value.
- No paper sheet has such a zone. This is a deliberate, user-requested reading aid, and the only
  addition to a sheet's diagram.
- It applies only to diagrams, never to the value or rating tables.

**The diagram elements must never be confusable.** They differ in meaning, colour, shape and
position:

| Element | Meaning | Colour | Shape | Position |
|---|---|---|---|---|
| Average zone | the manual's „durchschnittlich“ range (the same for every child) | white `#FFFFFF`, no border | wide filled strip, behind everything | spans the diagram across all rows or columns |
| Range (3+ only) | the child's PR-Band (68 %) | grey `#8C8C8C` | two round 7 px dots joined by a line | left of the column centre |
| Value | the child's PR | black `#12100C` | single 8–9 px square, sharp corners | on the PR position (profile); right of the column centre (corridor) |

- The dots and the square never overlap each other, and neither is white. So both stay visible on
  the zone and on the grey background alike.
- **A legend on every diagram that names every drawn element.**
  - 3+ corridor, two rows: „Prozentrang des Kindes“ with the square, then „Prozentrang-Band (68 %):
    unterer und oberer Rand“ with the joined dots, then „weiße Fläche: Durchschnittsbereich des
    Tests (PR 25–75, „durchschnittlich“) · für alle Kinder gleich“ with a white swatch.
  - 2+/5+ Profil PR, two rows:
    - row 1: the square, with „Prozentrang des Kindes je Teilbereich · weiße Zellen: Werte des
      Kindes · grau: Beschriftungen und Bänder“
    - row 2: the swatch, with „weiße Fläche im Profil: Durchschnittsbereich des Tests (PR …) · für
      alle Kinder gleich“
  - 1+ Profil PR, one row: the square only, with the short „weiß: Werte des Kindes“ wording.
- If a band is missing or cannot be parsed, no dots are drawn. The value square is still drawn.

**DEMAT 3+ corridor gap.** On the paper sheet the corridor joins the band bounds of every column.
The app stores no subtest PR-Band (see ADR 0013, out of scope). So the report draws:

- band dots only for the Testgesamtwert
- the child's PR square in all four columns

The legend says that subtest bands are not stored. They are never looked up at render time, because
that would print values nobody validated. Once subtest bands are stored:

- each column gets its own pair of dots
- the upper dots of neighbouring columns are joined, and so are the lower dots
- the result is the paper corridor

**Grayscale only. White has exactly two uses.**

- No colour, and no category colour scheme.
- Every table cell and diagram area has a light grey background (`#EDEDED`) unless one of these
  applies:
  1. **Table cells: the child's own values.** White (`#FFFFFF`) marks every cell that holds the
     child's own actual result:
     - subtest RW and PR
     - total RW
     - PR (ges) and T (ges)
     - the child's category column in the rating-band table
  2. **Diagrams: the average zone of the PR axis.** This means the 3+ corridor and the 2+/5+
     Profil PR.
- In the rating-band table every other column, including „durchschnittlich“, stays grey. Band
  cells and labels stay grey.
- Colours used: ink `#12100C`, band dots and line `#8C8C8C` (3+ only), grid `#C8C8C8`.

**Band edges are derived at runtime from the KB, by probing `classify`.**

- `norms.band_segments()` classifies every integer from 0 to 100 and collapses the results into
  ordered segments. The result is cached per process.
- It feeds the rating-band table and the 2+/3+ average zone.
- The only exception is the cited 5+ PR constant described above.
- An upstream `lookup.py bands` subcommand is a follow-up. It could also expose PR equivalents for
  T-basis versions.

**One template for all versions, with a per-version layout block.**

- The section order is fixed:
  1. header
  2. rating line and rating-band table
  3. sheet table with its diagram
  4. subtest lines
  5. Hinweise
  6. Bemerkungen
  7. footer
- The sheet SVG comes from one of two layout functions (`rows_with_profile` or
  `columns_with_corridor`), chosen by `sheet_layout`.

**Subtest commentary.**

- PR basis: lists of the subtests above and below „durchschnittlich“.
- T basis: only „relativ höchste / niedrigste Teilbereiche“, never „Stärke/Schwäche“.
- Each list is capped at four entries, and each list is its own paragraph.

**Single-page guarantee by construction, verified manually.**

- Everything on the page is bounded.

  | Sheet SVG | Height | Contents |
  |---|---|---|
  | DEMAT 2+ | 372 px | 10 subtest rows, a 4-row Gesamtwert block, and a two-row legend because of the zone |
  | DEMAT 3+ | 318 px | |
  | DEMAT 1+ | 314 px | no zone |

- The worst case is DEMAT 2+ at about 940 px of the 1032 px printable height. That leaves about
  8.9 % slack and stays under the 95 % (980 px) test ceiling.
- The print view uses:
  - A4 portrait with 12 mm margins
  - a 10 pt base font
  - `print-color-adjust: exact`
  - a print-only `display: block` override for batch page breaks
- The footer is in normal flow.
- Rollout requires a Chrome print-to-PDF check of a named worst-case fixture. The guarantee covers
  the app print view only.

**The `person_id` is in the footer, and nowhere a name could leak.**

- Footer: `ID <person_id> · Bogen #N · …`. An unlinked sheet shows „ID nicht verknüpft (Bogen #N)“.
- The name is shown only when `STORE_RAW_NAMES` is on. Otherwise a write-in blank is shown.
- Name and klasse are escaped.
- Paths use `person_ref` and never the name.

**Storage** follows ADR 0007/0008.

- Each report is an append-only `child_reports` row plus a file on disk.
- `content_hash` is used for staleness.
- Only validated assessments get a report.
- The table is additive.

**UI.** A „Child reports“ tab in the Class Profile panel offers:

- generate, preview and download per child
- generate-all
- print-all (one child per page)

## Alternatives rejected

- **Re-deriving the rating from `rw_total` via `_gesamt`.** It flips the verdict on two of the three
  real sample sheets.
- **Detecting the table the evaluator used by re-lookup and match.** It is ambiguous, costs three
  lookups per child, and duplicates `norm_mapping`.
- **Printing `resolve_norm_group()`'s `_gesamt` name in the header.** It claims a table the values
  may not come from.
- **A category colour scheme.** The user rejected colour, for three reasons:
  - the paper sheets have none
  - colour implies a judgement beyond the manual's words
  - colour prints poorly in black and white
- **Highlighting the „durchschnittlich“ column of the rating-band table in white.** This was a
  misreading of the request and was withdrawn. The average highlight belongs to diagram axes only.
  In tables, white stays reserved for the child's own values.
- **Estimating a PR range for the DEMAT 1+ zone.** Candidates were the normal-curve conversion of
  T 40–60 (about PR 16–84) or borrowing the 5+ table. The KB states neither for 1+, and the user
  ruled out estimating. So 1+ has no zone.
- **Hardcoding the 2+/3+ zone edges.** This duplicates the KB. They use the same `band_segments()`
  probe as everything else, and a test pins the expected values. The 5+ constant is the documented
  exception, because the CLI offers no PR scale for 5+.
- **Using the norm tables' „grau unterlegt … mittlere 50 %“ rows as the zone.** That is a different
  concept from the manual's rating category.
- **Shading the zone in a different grey instead of white.** The user asked for white. Another grey
  would also be hard to tell from `#EDEDED` when printed.
- **One single zone rectangle over the Profil PR.** It would paint over the row borders inside the
  zone. Instead there is one inset rectangle per subtest row, so the row borders stay visible.
- **Synthesized charts not on the sheet** (a separate range axis, a subtest bar profile). The user
  rejected them: the report must look like the sheet the teacher knows.
- **An extra „Gesamtwert“ row under the 1+/2+/5+ Profil PR.** It plotted the overall value and its
  band, using its own T scale (10–90) on T-basis versions. It was rejected because:
  - no scanned sheet prints such a row
  - on T-basis versions it mixed two scales in one diagram
  - the band is already printed as a number in the Gesamtwert block
- **Drawing the band as a filled bar with end caps.** The user asked for the sheet's own convention
  instead: dots at the band's edges, joined by a line. A bar also looks heavier than the value it
  frames, and it would now also be confusable with the zone.
- **Drawing range and value in the same style**, or placing the value marker on the band. The reader
  could not tell "where the true value likely lies" from "what the child scored".
- **Looking up DEMAT 3+ subtest bands at render time.** It would print values nobody validated.
- **An LLM prose stage.** It breaks the one-page guarantee and adds cost.
- **Version-specific templates.** Only the sheet layout differs between versions, and that is
  isolated in one SVG function per layout.
- **Hardcoding band edges, or importing `lookup.py`'s `BANDS`.** This duplicates the KB or couples
  to it. The CLI is the only contract, with the single cited 5+ PR exception.
- **An all-SVG page, or an HTML/PDF artifact.** Markdown with inline SVG stays editable.
- **A `position: fixed` footer.** It repeats on every page in batch printing.
- **Rendering on the fly without storage.** An issued report must stay retrievable exactly as
  issued.
