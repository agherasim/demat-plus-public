# ADR 0013 — The app looks up PR/T norm values the sheet leaves for the evaluator, from the knowledge base, with per-cell provenance

- Status: Proposed
- Date: 2026-09-27
- Extends: ADR 0005 (§9.4A), ADR 0008 (KB rule 2), ADR 0010
- Amends: ADR 0003 / ADR 0005 §5's "transcribe only what is printed" contract, narrowly (see Decision)

## Context

A DEMAT cover sheet is meant to be completed in two steps by two different people or at two
different times.

**The teacher** scores the test booklet and writes each subtest Rohwert (RW) onto the cover
sheet. Usually the teacher also writes their sum, RW(ges). The column headers show the split:
the sample DEMAT 2+ sheet labels its columns „RW (aus Testheft)“ and „PR (aus Handbuch)“.

**Whoever evaluates the results** then looks up the PR, PR-Band, T-Wert and T-Band in the
manual's norm tables. That step needs the manual and a decision about which norm table
applies.

So a sheet with RWs written and the norm cells empty is **not** incomplete, badly scored or
damaged. It is the sheet in its normal state when it reaches us. We are the evaluator, and
doing the lookup is part of what this app is for.

We verified this against a real sample: two DEMAT 2+ Form A sheets from class 3c, September
2026. All ten subtest RWs and RW(ges) are written. The PR column, the PR profile and the four
Gesamtwert norm fields are empty, as the workflow expects.

The app currently treats this normal input as a defect:

- **Extraction marks the empty cells as problems.** `reference.md`, shared by every
  extraction skill, says an empty cell is `null` **with `"low"` confidence and an
  `illegible_notes` mention**. That puts a cell nobody was meant to fill in the same bucket as
  a cell the model could not read. On a typical sheet, every norm cell then raises a blocking
  `low_confidence` flag.
- **Downstream consumers get nothing to work with.** `review_validation._check_norm_mapping`
  has no PR to check. `_check_note_matches_mapping` and the Class Profile's classification
  both need `pr_total`/`t_total`, so the child ends up unclassified.

The lookup the evaluator is supposed to do is exactly what the knowledge base provides.
`norms.norm(norm_test, scale, group, raw)` returns `pr`, `pr_band`, `t_wert`, `t_band` and a
cell `status`. Its docstring already names this use: "fill an overall value the sheet did not
carry but whose Rohwert it did". Nothing does it yet. Today, KB output is only ever compared
against stored values; it is never stored itself.

The KB does not publish the same fields for every test. The spec's per-test table has the
detail. For example, DEMAT 1+ and 5+ publish no PR-Band. DEMAT 1+/2+ subtests have a PR only.
DEMAT 5+ Subtest B is not normed. Several DEMAT 1+ low-RW rows print no PR, or print `≤1`.
The design therefore also has to say what happens when the manual itself has no value to
look up.

This is the first time a norm value goes into the database as data rather than as a check.
So this ADR decides who may write it, when, and how it stays distinguishable from what was
handwritten on the paper.

## Decision

**Norm lookup is a pipeline step in its own right, and only Python performs it, only from
`lookup.py`.** The new module `demat/norm_completion.py` fills each norm cell the sheet left
for the evaluator. It gets the values through `demat/reports/norms.py`, which stays the only
caller of `scripts/lookup.py`. KB rule 2 (ADR 0008) is unchanged: no LLM produces, sees or
chooses a norm value.

The step never overwrites a value that was written on the sheet, including one the model
could not read, and never overwrites one a reviewer entered. ADR 0003's "never auto-correct"
and ADR 0005 §9.4A's "flag, never correct" therefore still hold: filling a cell that was
meant to be filled replaces nothing.

**The model observes the cells; Python does the lookup.** The extraction contract gains one
observation per norm-bearing cell, `SubtestScore.pr_state` and `MetricValue.value_state`,
each `"written" | "blank" | "illegible" | None`. The model reports only what it sees: an
empty cell, or a written but unreadable one. It still never infers a value, and the "never
infer a PR from an RW" rule in `reference.md` stays word for word.

- Python looks up a value only for cells reported `"blank"`.
- `"illegible"` cells are never filled. Someone wrote a value there, and a computed value
  must not pose as it.
- `None` cells are never filled either. `None` comes from a stub skill, or from a response
  written before this change.

Two instructions to the model change with this:

- `reference.md` stops treating an empty cell as a confidence problem. Confidence describes
  how legible the written content is.
- The runtime prompt contract (`test_registry.prompt_contract`) names the new field, so the
  prompt and `reference.md` say the same thing.

**Provenance is stored per cell, next to the value.** `subtest_scores.pr_provenance` and
`assessment_metrics.value_provenance` are nullable TEXT columns, added in place. Each takes
one of five values:

- `sheet`: handwritten on the sheet. NULL on legacy rows means the same thing; there is no
  backfill, following ADR 0010.
- `blank`: left for the evaluator and not yet looked up. The usual reason is that no norm
  table is recorded yet.
- `computed`: looked up by the app.
- `unavailable`: left blank because the manual publishes no value for this cell at this RW.
  Examples are DEMAT 1+ `leer_im_original` PRs, PR-Band on DEMAT 1+/5+, and DEMAT 5+
  Subtest B. Empty is the correct state for these cells, not something to fix.
- `manual`: set or changed by a reviewer.

Rules for these values:

- Completion owns `blank`, `computed` and `unavailable` cells and re-evaluates them on every
  run, because the RW or the norm table may have changed.
- A reviewer edit that changes a value makes the cell `manual`, and completion never touches
  it again.
- Values are validated in Python, not with a `CHECK` constraint, for the same reason ADR 0010
  gives.

A single flag on the assessment would not be enough. The UI has to mark individual cells, a
re-run has to know which cells it owns, and `raw_response_json` is immutable, so it cannot
record anything after the insert.

**The norm variant is the asserted `assessments.norm_variant`, and the table is the combined
one (`_gesamt`).** Completion resolves the group with `norms.resolve_norm_group()`, the same
path `_check_norm_mapping` uses. If no variant is recorded, completion writes nothing,
because ADR 0010 forbids guessing the table. The cells stay `blank`, and review asks the
reviewer to set a Normtabelle.

For the gender-split tests (DEMAT 1+ and 2+), the combined table is always used. The KB's own
guidance settles this (`references/allgemein.md`): it treats the gender-specific norms as
mainly a research instrument and recommends the combined norms for everyday use. It is also what
`resolve_norm_group` returns, it does not depend on a gender checkbox the model may have
misread, and it is the series ADR 0008 uses for class reports.

Every lookup message names the table. The table is deterministic, so no extra column is
needed to record it: `norm_variant` plus this rule reproduces it.

**Completion runs whenever its inputs may have changed, and always before review
validation.** It runs only on `needs_review` rows, at three points:

1. at ingestion, in `pipeline._process_sheet`, after `save_assessment` and before
   `run_review_validations`;
2. after a review-panel "Save changes" that actually changed a field, such as the reviewer
   setting the Normtabelle or correcting an RW;
3. at the start of "Run validation".

Completion is idempotent. Completion writes do not stamp `edited_at`, because a system lookup
is not a human edit. As a result, `review_validation_edited_at` cannot see these writes, so
completion must only ever run immediately before validation, or as part of a save that has
already stamped `edited_at`. Like review validation, it is best-effort: a failure never loses
the saved row.

**Missing infrastructure degrades completion; it never clears values and never guesses**
(ADR 0005 §9.6).

- **KB unavailable:** completion writes nothing and leaves existing values alone.
- **`druckfehler_vermutet` cells:** never stored. The KB marks status per row, so the whole
  row is refused. On DEMAT 5+ Gymnasium RW 22, for example, only the T-Band is suspect, but
  PR and T are refused too.
- **`unsicher` cells:** stored, and the uncertainty is shown in the message.
- **Non-numeric values:** a PR published as `≤1` (DEMAT 1+ Gesamtwert) is stored verbatim in
  the text column `value_raw` with no numeric value, exactly as a scorer would write it. It is
  never coerced into a number.

**RW(ges) may be filled by arithmetic, not by a norm lookup.** According to the manuals the
teacher writes RW(ges), but if it is `blank`, the app fills it with the sum of the subtest
RWs. It does so only when:

- the definition has a `[[rules.sum]]` rule for it;
- every defined subtest is present exactly once with a numeric RW; and
- no undefined subtest was emitted.

This sum is correct for all four definitions: each one's `max_possible` equals the sum of its
subtests' `rw_max`, including DEMAT 5+'s unnormed Subtest B.

> **Amended by ADR 0015.** DEMAT 3+'s real sheet fills RW per Aufgabentyp (task type), not per
> subtest, so "sum every defined subtest" is not sufficient for that test — see ADR 0015 for the
> grouped-sum rule this required.

**How it is signalled: as a normal step, with a clear record, and not as an alarm.**

- **Per assessment:** a flag code `norm_values_computed` with a new flag severity, `info`.
  - It never blocks. `has_blocking_flags` ignores it, just as it ignores warnings.
  - Completion adds the flag when at least one `computed` cell exists and removes it
    otherwise.
  - It shows as an inbox badge and as one neutral line in the Status section, for example
    „Normwerte aus Normtabelle k3_gesamt nachgeschlagen“. It is not a warning.
- **Per cell:** a new read-only review check, `norm_completion`.
  - It emits one `info` finding per `computed` cell (`field_ref` `subtest:<key>` /
    `metric:<key>`). The panel collapses these into one line per table.
  - The only `warning` it emits is for a cell still `blank`. There is an actionable reason
    for it: no Normtabelle is set, the KB is unavailable, or the RW itself is missing or not
    in the table.
  - `unavailable` cells produce no finding, because an empty cell is correct there.
- `Severity` gains `"info"`. `has_errors` is unchanged, so nothing here blocks Validate.

**Validate stays a single click, and the result is `validated_ok`.** The lookup is the
sheet's intended workflow, not an exception that needs a separate acknowledgement. The
reviewer's Validate click covers the whole row against the source. This includes the looked-up
values, which are shown and labelled with their table. Completion is not a human edit, so a
row whose only non-sheet values are `computed` finalizes as `validated_ok`. The provenance
columns permanently record that the values were looked up by the app. ADR 0007 would keep an
audit fact like this there, not in a terminal state whose meaning is "a human changed this".

**`norm_mapping` is not changed, and it does not validate looked-up values.** For a `computed`
cell it re-derives the same number through the same lookup, and on gender-split tests it
accepts either candidate table anyway. Its remaining value for such cells is catching drift,
for example a KB refresh after completion. Its real job remains checking PRs that someone
*did* write by hand. `note_matches_mapping` gets a real input: it can now judge the note
against a classification of a Python-computed `pr_total`/`t_total`, exactly the split ADR 0008
prescribes.

**Out of scope, recorded so it isn't mistaken for an oversight.** The KB also publishes
subtest T-Werte, and PR-Bands for DEMAT 3+, and T-Werte and T-Bands for DEMAT 5+ subtests A
and C. The DEMAT 3+ cover sheet's profile uses the subtest PR-Band. `SubtestScore` only
stores `pr`, so the app cannot store these values yet. Adding them is a schema change (ADR
0005) for a follow-up, once real 3+/5+ sheets have been analyzed.

## Alternatives rejected

- **Treating the empty norm cells as missing data to recover from.** This is today's implicit
  model, and it gets the paper workflow backwards. It produces flags on every correctly
  prepared sheet and frames the app's core job as error recovery.
- **A sheet-level "unscored mode" set by the extraction skill.** It makes the model the
  gatekeeper of a computation, and it cannot express a mixed sheet where some PRs are written
  and some are left. Asking for one observation per cell gives Python everything it needs and
  asks the model only what it can see.
- **A user-chosen mode at upload or in review.** This is the normal case, not a mode. A switch
  would have to be on for almost every sheet. It would also fail on mixed batches, for the
  same reason ADR 0010 rejected a batch-wide selector.
- **Detecting from data shape alone (RW present, PR `null`), with no observation from the
  model.** `null` today means both "left for the evaluator" and "written but unreadable".
  Filling the second case would replace a scorer's value, which might come from a different
  table, with ours.
- **Computing in `validate()` at extraction time.** `validate()` sees only the in-memory
  `ExtractedSheet`, has no norm variant, and must run without the KB. Its output is also
  frozen, while the lookup has to re-run when the reviewer sets the Normtabelle.
- **Computing inside `review_validation`.** Its results are persisted as findings and never
  touch data. A check that writes the values it then checks would blur the line between
  computing and judging.
- **Computing only on read (display without storing).** Class Profile, CSV and
  `note_matches_mapping` all read stored values. Each would need its own lookup, and a
  validated report would change silently whenever the KB changed.
- **An assessment-level flag with no per-cell provenance.** It cannot mark cells, cannot tell
  a re-run which cells it owns, and cannot survive an edit.
- **Gender-specific tables where the gender is known.** The KB's guidance is to use the
  Gesamtnormen in everyday use. Gender tables would also make the value depend on a checkbox
  the model may have misread. If a school later needs within-gender comparison, the change is
  one resolution call plus the message text.
- **Checking both candidate tables and filling only when they agree.** They almost never
  agree. In the sample, the overall PR is 10 in `k3_gesamt` against 13 in `k3_mädchen`, and 93
  against 90 in `k3_jungen`. In practice nothing would be filled.
- **Collapsing `unavailable` into `blank`.** Both mean an empty cell, but only `blank` is
  something a reviewer can act on. Merging them would put a permanent warning on every DEMAT
  1+/5+ PR-Band and every 5+ Subtest B.
- **An explicit "I checked the looked-up values" acknowledgement before Validate.** The lookup
  is the intended workflow, so an extra gate would tax every normal sheet. That is the cost
  ADR 0007 kept off Validate.
- **`validated_with_changes` for rows with looked-up values.** That state means a human
  changed data. Using it for the app's normal lookup would make it true of nearly every row,
  and it would lose its meaning.
- **Coercing `≤1` to 1 (or 0).** That would invent a precision the manual does not print.
  Storing the literal is faithful and matches what a scorer would write.
- **A standalone "Look up" button.** Completion writes do not stamp `edited_at`, so a
  standalone run could leave validation results that look current but are stale.
