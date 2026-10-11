# ADR 0003 — Local SQLite store, generic subtest model, and explicit review workflow

- Status: Accepted
- Date: 2026-09-20

## Context

Extracted results must persist locally and be browsable in a Streamlit "Results" tab.
The four DEMAT versions (1+, 2+, 3+, 4+) share a shape — a list of subtests with a raw
score and a percentile, plus an overall block — but not a subtest list. Streamlit reruns
the whole script on every widget interaction and runs it in a worker thread, both of
which constrain how persistence and triggering are written.

## Decision

**Storage.** A single local SQLite file at `data/demat.db`, created by an idempotent
`schema.sql` executed at startup. `data/` and `*.db` are added to `.gitignore` — children's
extracted data must never be committed.

**Connections.** No module-scope or cached connection. Every operation opens a short-lived
connection through a `contextmanager` (`sqlite3.connect(DB_PATH)`, `row_factory = sqlite3.Row`,
`PRAGMA foreign_keys=ON`, `PRAGMA journal_mode=WAL`). A module-scope connection raises
`ProgrammingError` because Streamlit executes the script on a worker thread; a cached
connection with `check_same_thread=False` would work but concentrates locking bugs.

**Subtests are rows, not columns.** `subtest_scores` is a child table of `assessments`,
keyed `(assessment_id, position)` — **position, not abbreviation**. A handwritten or
smudged table can yield two rows the model reads as the same abbreviation; with
`abbreviation` in the primary key that INSERT raises `IntegrityError` and kills the whole
assessment save mid-transaction, contradicting the never-drop-a-record rule below.
`abbreviation` is a plain indexed column, and repeated abbreviations raise a
`duplicate_subtest` flag instead of an exception. A seeded `subtest_catalog` per test
version supplies canonical names and `rw_max` for validation and display ordering, but an
unknown abbreviation is stored anyway and flagged, so a new DEMAT version never loses data.

**Idempotency by page key.** `assessments.source_page_sha256` is UNIQUE and holds
`sha256(f"{file_sha256}:{page_index}")` — the hash of the *source page identity*, not of the
rendered PNG. Hashing the PNG would make the key depend on DPI and the downscale cap, so
changing a render setting would silently re-extract and re-bill the entire corpus. This is
also more robust than a `(person, test, date)` natural key, since the date itself may be
misread.

**Review workflow.** Every assessment carries `review_status` in
`('ok','needs_review','rejected')`, a `flags` JSON array of codes, and a `review_notes`
free-text column carrying the detail a human needs to resolve a flag (which ID collided
with which key, which abbreviation repeated, what the raw name was on an unlinked sheet).
Codes alone are not resolvable; hence both columns. Low model confidence, failed
validation, an ID collision, or a missing name produce `needs_review` — never a dropped
record and never a silent write of a wrong value. The Results tab shows all rows by default
with a status filter and a visual marker for flagged rows.

**Triggering.** Extraction runs behind an explicit `st.button("Extract")`, never on upload,
because Streamlit re-executes the script on every interaction and an upload-triggered call
would re-run the agent on each rerun. In-flight state is keyed in `st.session_state` by file
content hash.

**Name storage.** The raw name is stored in a separate `person_names` table, nullable and
never joined into the default Results view; it exists only to let a human resolve a flagged
collision. A config flag (`store_raw_names = false`) suppresses it entirely.

## Alternatives rejected

- **One wide table with fixed columns per DEMAT 2+ subtest.** Simplest to query, but breaks
  the moment a DEMAT 3+ sheet is uploaded, and the task explicitly calls for flexibility.
- **`PRIMARY KEY (assessment_id, abbreviation)`.** Naturally expresses "one row per
  subtest", but converts a model transcription quirk into a hard save failure.
- **Hashing the rendered PNG for idempotency.** Simpler to compute at render time, but ties
  the dedup key to render settings.
- **CSV / Parquet files.** No uniqueness constraints, no cheap upsert, and the task asks
  for SQLite.
- **A server database (Postgres).** Adds infrastructure to a single-user local app handling
  data that should stay on the machine.
- **`@st.cache_resource` connection singleton.** Standard Streamlit idiom and fine for
  reads, but a write-heavy pipeline running concurrently with UI reruns makes short-lived
  connections plus WAL the lower-risk default.
- **Auto-correct failed validations (e.g. rewrite the total to match the sum).** Hides
  transcription errors in data about children; flagging for human review is the only
  acceptable behavior.
