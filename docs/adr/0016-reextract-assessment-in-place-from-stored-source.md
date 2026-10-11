# ADR 0016 — "Re-extract" replaces an assessment's row from the stored source page in one transaction, keeping the child link and the Normtabelle

- Status: Proposed
- Date: 2026-09-27
- Extends: ADR 0007 (status lifecycle, stored source PDF), ADR 0006 (person identity),
  ADR 0010 (per-assessment `norm_variant`), ADR 0015 (DEMAT 3+ re-extraction rollout)
- Related: ADR 0001 (deterministic `person_id`), ADR 0005 §9.6 ("never crash, never guess")

## Context

ADR 0015 fixed the DEMAT 3+ extraction contract. The fix only helps **future** extractions.
Some rows already in the database (for example assessments 6 and 7) were extracted under the
old contract. They dropped the Aufgabentyp rows, so nothing is stored that norm completion
could work from. The only remedy is to read the page again.

Today that takes two manual steps:

1. **Delete** the row. The inbox batch action calls `store.delete_assessments`.
2. **Re-upload the whole original PDF.** `store.page_already_extracted` skips every page that
   still has a row, so only the deleted page is extracted again.

The source is already on the server. `source_files.stored_pdf_path` holds
`data/uploads/<sha256>.pdf`, and the rendered page PNG usually still exists. The pipeline also
already has the per-page unit of work, `pipeline._process_sheet`. It has no entry point for "this
page, which already has a row".

Doing "Delete, then extract" naively has five defects:

- **A failed extraction loses the row.** This happens with an agent error, triage saying "not a
  sheet", a stop, or a missing runner.
- **`delete_assessments` removes files the new extraction just wrote.** It unlinks
  `PAGES_DIR/<page_key>.png` and `RUNS_DIR/<page_key>(.triage).json`. Those are exactly the
  paths `extract_sheet` writes, because both are keyed by `page_key`.
- **A stale run file can be passed off as fresh.** `agent_runner._read_output_json` reads
  whatever file is at the output path, and nothing clears it before a run. If the agent fails
  to write a new file, the old JSON is parsed and saved as a "new" extraction.
- **Identity is recomputed from the new name read.** That creates a phantom second person when:
  - the new read spells the name differently;
  - the reviewer had corrected the name (`rename_assessment_person` keeps `person_id` and only
    adds a spelling);
  - the child had a disambiguated id (ADR 0006 known limitation: a recomputed id re-merges into
    the plain one).
- **The reviewer's Normtabelle choice (ADR 0010) is lost.** It is stored per assessment, not
  per file.

## Decision

**1. A new batch action, "Re-extract", with the entry point
`pipeline.reextract_assessments(ids, runner, …)`.** It reuses `_process_sheet` through a
keyword-only `replace=` argument. It does not copy the extract → validate → save → complete →
review-validate sequence. `process_pdf`/`process_pdfs` are unchanged.

**2. Extract first, then swap atomically.** The existing row is not touched until a savable
extraction exists.

- `save_assessment(…, replaces_assessment_id=…)` runs these steps in **one** transaction:
  1. `DELETE FROM assessments WHERE assessment_id = ? AND source_page_key = ?`, which cascades
     to metrics and subtests;
  2. person resolution;
  3. `INSERT`.
- If the DELETE matches no row (the row was deleted or changed meanwhile), the call raises.
  The app never resurrects a row the user has just deleted.
- Any failure rolls back, and the old row stays byte-identical. The failure cases are:
  not a sheet, error, stop, runner unavailable, the save raising, and a `UNIQUE` race.
- The page PNG and run JSON are **not** deleted, because the new extraction owns those same
  paths.
- Only the old id's `review_validation_<id>` run directory is removed, after commit.

**3. Clear the stale run outputs before extracting.** `RUNS_DIR/<page_key>.json` and
`.triage.json` are unlinked before `extract_sheet` runs. An agent that writes nothing then
fails loudly, instead of silently reproducing the old extraction. The DB's `raw_response_json`
is the durable record, and these files are only a cache.

**4. The row gets a new `assessment_id`.** Nothing treats `assessment_id` as durable:

- `class_profiles.assessment_ids_json` is a historical record with no FK. A replaced id
  correctly marks the class "stale", because the data changed.
- The inbox selection and the `?assessment=` URL parameter are session state. They are
  remapped from old id to new id when the batch finishes.
- Checkbox keys `inbox_select_<id>` are dropped.
- Reports and the CSV key on `person_id`. They fall back to `unlinked #<id>` only for unlinked
  rows.

Keeping the same id would mean an `UPDATE` in place, plus rewriting child rows under the same
id and resetting a dozen lifecycle columns. That second write path diverges from
`save_assessment`, and gains nothing.

**5. The child link is kept, not recomputed.** If the old row had a `person_id` and that
`persons` row exists, the new row keeps it. It is the same physical page (same
`source_page_key`), so it is the same child by construction. That is a fact, not a guess, and it
matches `rename_assessment_person`'s rule that "`person_id`, once allocated, is permanent".

- ADR 0006's gender-mismatch (§3) and duplicate-date (§4) checks still run on the kept id. They
  run after the in-transaction DELETE, so the row being replaced never counts as its own
  duplicate.
- **New blocking flag, `reextract_name_mismatch`.** It is raised when the new read's
  normalization key matches none of these:
  - the person's `normalization_key`, with the ADR 0006 `#X` disambiguation marker stripped;
  - any normalized `person_names` spelling for that id.

  The link is still kept, and the reviewer decides. The new spelling is **not** added to
  `person_names` in that case.
- If the old row was unlinked (NULL), or its person no longer exists, the normal
  first-allocation path runs. That path is `_resolve_person_link` plus one
  `BatchIdentityTracker` per re-extract batch, the same as an upload.

**6. Only `norm_variant` is carried forward.**

- The old row's `norm_variant` is re-applied if it is valid for the **new** extraction's
  `norm_test`. If it is not valid, it is left unset and the reason goes into `review_notes`.
  This is the same "never stamp a variant onto the wrong test" rule `_process_sheet` already
  applies.
- Nothing else is carried: not field edits, the note, status, `edited_at`, `finalized_at`, or
  review findings. Those were made against the old transcription, and replaying them onto a new
  one would be guessing.
- The new row starts as `needs_review` and unread. Its `review_notes` records one audit line:
  the old id, its status, and whether it had been edited or finalized.

**7. Re-extract is allowed for every status. It uses Delete's two-button confirmation.**

- It is strictly less destructive than Delete, which is already allowed on any row. Blocking it
  for finalized rows would push users back to the clumsier Delete + re-upload path.
- The confirmation warning states counts: how many selected rows have manual edits, how many are
  finalized or rejected, and that the child link and Normtabelle are kept.
- It is not type-to-confirm. Delete is not type-to-confirm either (`app.py` shows a warning plus
  Cancel/Confirm).

**8. Execution is a sequential loop on one background thread, with a `stop_event` and a
fragment-scoped progress poller.** The expected volume is a handful of rows. The following are
deliberately not reused:

- `process_pdfs`'s executor loop, which `tests/test_pipeline_stop.py` pins;
- the Upload page's whole-app `sleep`/`rerun` poll, which would rerun the review panel twice a
  second.

The rest of the behaviour:

- A `RunnerUnavailableError` aborts the batch. Per-row results are published to a
  caller-supplied sink as each row finishes, so the UI can still remap and report the rows
  that were already replaced.
- Stop takes effect between rows and inside an in-flight run. Both leave the untouched rows
  unchanged.
- While the batch runs, a row that is part of it renders read-only in the review panel. A
  concurrent manual edit therefore cannot race the swap.

**9. Source resolution degrades gracefully and never guesses.** The page image comes from the
first of these that succeeds:

1. `stored_pdf_path`, if the file exists **and** its sha256 equals `source_file_sha256`, and
   `source_page_index` is in range. The single page is re-rendered to its usual
   `PAGES_DIR/<page_key>.png`.
2. `UPLOADS_DIR/<sha>.pdf`, under the same checks. This covers a moved `DEMAT_HOME`
   (ADR 0009).
3. The existing page PNG, which is exactly the image that was extracted before. This is used
   only when the stored `source_page_key` verifies against `sha256(file_sha256:page_index)`.
4. Otherwise the row is skipped with `source_unavailable` and left untouched. That includes a
   row with a NULL `source_file_sha256`, which cannot be verified. The UI reports this per
   row.

## Alternatives rejected

- **UI-only "Delete, then re-run extraction".** It loses the row on any failure, deletes the new
  run files, and recomputes identity.
- **Extending Delete with a "Delete and re-extract" variant.** It has the same failure-ordering
  problem. It also hides a data-preserving repair behind a destructive verb.
- **Keeping the same `assessment_id` (UPDATE in place).** It needs a second, divergent persist
  path next to `save_assessment` for no benefit. No consumer depends on id stability.
- **Recomputing `person_id` from the new name read, as an upload does.** It creates phantom
  duplicate children on a spelling difference, undoes name corrections, and re-merges
  disambiguated children.
- **Blocking re-extract for edited or finalized rows.** Delete already allows it, so the block
  would only force the manual workaround. The explicit counts in the confirmation are the
  safeguard.
- **Replaying manual edits onto the new extraction.** The edits refer to cells of the old
  transcription. Mapping them onto a new transcription is guessing (ADR 0005 §9.6).
- **Routing through `process_pdfs` / a thread pool.** It renders every page of the PDF, needs a
  `PdfInput`, and changes a tested concurrency loop, all for a few rows.
- **Relying on the agent to overwrite the run file.** A silent failure would then reuse the old
  JSON. Unlinking first is the only safe contract.
- **Always reusing the existing PNG.** The stored PDF is the source of truth, and re-rendering
  picks up current render settings. The PNG is only a fallback, because it is the same image
  that was extracted before.
