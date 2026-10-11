# ADR 0007 — Assessment status lifecycle and the Inbox screen

- Status: Accepted
- Date: 2026-09-20

(The file name keeps its original slug; the decision no longer gives documents a lifecycle of
their own.)

## Context

A new **Inbox** screen is added alongside the existing "Load Assessments" page: a list of
assessments awaiting review, and a panel showing the source document next to the extracted
fields, editable inline. Load Assessments keeps ownership of upload/extraction; the Inbox owns
review only, with no uploader of its own. Editing must leave a durable record that a human
changed the data, and only an explicit validation action may finalize an assessment.

Three facts about the current code shape this:

- **Uploads are not retained.** `pdf_render.render_pages` writes per-page PNGs to `data/pages/`
  and drops the PDF bytes; `source_files` is only written during processing. Nothing exists to
  open when the user wants to check an edit against the original document.
- **A review workflow already exists** (ADR 0003): `assessments.review_status` in
  `('ok','needs_review','rejected')`, a `flags` JSON array of validation codes, and
  `review_notes` carrying the detail a human needs to resolve a flag.
- **The Inbox is a list of children, not of files.** One PDF contains several children's sheets,
  so a per-document list would not be the unit the user works in.

The existing `data/demat.db` is to be deleted rather than migrated.

## Decision

**Four states on `assessments.status`** (renamed from `review_status`, whose triple it
replaces):

- `needs_review` — the only non-terminal state. Every extracted assessment starts here.
- `validated_ok` — terminal. Human confirmed against the source; nothing was changed.
- `validated_with_changes` — terminal. Human confirmed against the source after changing at
  least one field.
- `rejected` — terminal. Unusable sheet or scan.

**`validated_with_changes` is permanent.** It is not a warning that a later action clears; it is
the record that this row's data was touched by a human and is no longer purely what the model
read off the sheet. Nothing downgrades it to `validated_ok`.

**Editing never changes status.** Saving an edited field leaves the row in `needs_review`,
stamps `edited_at`, and appends the detail to `review_notes`. Only the explicit **Validate**
action commits a terminal state, and which one is decided solely by `edited_at IS NULL`.
Terminal is final: reopening a finalized assessment is out of scope. **Validate is a single
click; Reject takes a two-click confirm** — Validate is the common, low-harm path and a confirm
would tax every row, while Reject is rare, irreversible, and drops a child's data out of the
results.

**The Inbox is two columns, and read/unread is styling, not structure.** The left column is one
list of every `needs_review` assessment — one row per child assessment, `created_at DESC` across
the whole list. Rows with `opened_at IS NULL` render **bold**; opening a row stamps `opened_at`
and it un-bolds *in place*, never changing position. The right column is the review panel for
the selected row. `opened_at` and `edited_at` are timestamps recording what happened to a
`needs_review` row, in the same spirit as `created_at` — not lifecycle states, and not a
partition of the UI.

**Documents are infrastructure, never a list.** `source_files` and a retained
`data/uploads/<sha256>.pdf` (`source_files.stored_pdf_path`) exist to drive extraction, back the
`page_already_extracted` dedupe, and supply the original PDF the review panel offers for
checking an edit against the source. Import outcomes — not-a-sheet pages, per-page errors,
failed PDFs — are reported inline for the current import run. Import runs behind an explicit
"Import & extract" button: ADR 0003's triggering rule exists because Streamlit re-executes the
script on every interaction, and an explicit button satisfies it even though extraction now
happens at import time.

**Edits write projections, never provenance.** `raw_response_json` is immutable after insert.
Edits rewrite `assessments` scalars, `assessment_metrics` and `subtest_scores` (each keyed by
`position`, per ADR 0003), applying the same `coerce_float` / `value_type` rules
`save_assessment` uses.

**`validate()` no longer decides a status.** `ValidationResult.review_status` becomes
`has_blocking_flags: bool`; `flags` and `review_notes` keep their ADR 0003 meaning and drive the
Inbox row badge. `flags` carries no edit marker — `edited_at` does that.

**No migration.** `schema.sql` is changed directly and `data/demat.db` (+ `-wal`/`-shm`),
`data/pages/` and `data/runs/` are deleted as part of the change; `data/.salt` is left in place
so existing `person_id` derivation is unaffected. Shipping this discards every previously loaded
assessment — an explicit instruction, recorded here because it is destructive.

**Name editing is out of scope.** The raw name lives in `person_names` and `person_id` is a
salted hash (ADR 0001, ADR 0006) whose recomputation can hit the `id_collision` path.
`person_id` is read-only in the Inbox, and unlinked rows render a fallback label rather than a
blank.

## Alternatives rejected

- **A `manually_edited` flag on an otherwise-clean `validated` state, cleared by validating.**
  An earlier draft of this ADR. Rejected: the fact that a human changed data about a child must
  survive validation, not be erased by it. A permanent terminal state cannot be un-set by a
  later click; a flag can.
- **Routing unopened and opened assessments into two separate columns.** Also an earlier draft.
  Rejected: it splits one queue into two piles the user must scan separately, and makes
  selecting a row teleport it to another column. Bold-vs-regular carries the same information
  inside one `created_at DESC` list.
- **A fifth `new` state for unopened rows.** Would make read/unread a lifecycle value that means
  only "not clicked yet", and would double the terminal-transition table. `opened_at` expresses
  it with a timestamp, consistent with `edited_at`.
- **A separate status column alongside `review_status`.** Two columns that can disagree and two
  filters to keep in sync — the parallel system the request ruled out.
- **A boolean `has_warning` / `manually_edited` column.** Carries neither which field changed
  nor when; `edited_at` + `review_notes` carry both, and the terminal state carries the verdict.
- **Editing auto-advancing the status** (e.g. an `edited` state). Would make an accidental
  keystroke a lifecycle event and would lose the validation flags that put the row in triage.
- **A documents column in the Inbox.** One PDF holds several children's sheets, so the unit of
  work is the child; a document column would make the user triage files to get at children.
- **A confirm dialog on Validate too.** Symmetric, but it taxes the action performed on every
  row to guard against a mistake that is far less costly than a mistaken Reject.
- **Auto-extracting on file drop.** Streamlit re-executes the script on every interaction, so
  the agent would be re-invoked on each rerun (ADR 0003).
- **A backward-compatible migration** (`PRAGMA user_version` gate, `ALTER TABLE`, rebuild of
  `assessments` to widen its CHECK). Correct if data had to be preserved; it does not, and the
  rebuild — with FKs off and `assessment_id` copied by hand — was the riskiest part of the
  change for no benefit.
- **Keeping only the rendered page PNG as "the source".** Cheaper, but a downscaled single page
  is not the original document the user must check an edit against.
- **Editing `raw_response_json` to match corrected values.** Destroys the provenance record, by
  the same reasoning that rules out auto-correcting validation failures in ADR 0003.
- **Re-running `validate()` after each edit.** Requires rebuilding an `ExtractedSheet` from
  stored rows, and would let an edit silently clear a flag the human never resolved.
