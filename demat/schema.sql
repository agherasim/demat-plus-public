PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS persons (
  person_id          TEXT PRIMARY KEY,                -- 4-char base36
  normalization_key  TEXT NOT NULL UNIQUE,             -- name only, no klasse (ADR 0006)
  created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

-- optional, suppressible via config.STORE_RAW_NAMES
CREATE TABLE IF NOT EXISTS person_names (
  person_id  TEXT NOT NULL REFERENCES persons(person_id) ON DELETE CASCADE,
  raw_name   TEXT NOT NULL,
  first_seen TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (person_id, raw_name)
);

CREATE TABLE IF NOT EXISTS source_files (
  file_sha256     TEXT PRIMARY KEY,
  file_name       TEXT NOT NULL,
  page_count      INTEGER NOT NULL,
  stored_pdf_path TEXT,                                 -- data/uploads/<sha256>.pdf, for "check against source"
  status          TEXT NOT NULL DEFAULT 'imported'
                  CHECK (status IN ('imported','failed')),
  status_note     TEXT,                                 -- detail when status='failed', survives a rerun
  imported_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS assessments (
  assessment_id      INTEGER PRIMARY KEY AUTOINCREMENT,
  person_id          TEXT REFERENCES persons(person_id) ON DELETE SET NULL,  -- NULL when unlinked
  source_file_sha256 TEXT REFERENCES source_files(file_sha256),
  source_page_index  INTEGER NOT NULL,
  source_page_key    TEXT NOT NULL UNIQUE,             -- sha256(file_sha256:page_index), see identity/pipeline
  page_image_path    TEXT,
  test_version       TEXT,                            -- 'DEMAT 2+'. No FK to test_definitions: an unrecognised
                                                        -- sheet must still be storable (ADR 0005 §3).
  test_form          TEXT,                            -- 'A'
  extraction_skill   TEXT,                            -- which skill produced this row
  klasse             TEXT,
  gender             TEXT,
  test_date_raw      TEXT,
  test_date_iso      TEXT,
  norm_variant       TEXT,                            -- published norm table used, gender suffix stripped
                                                        -- ('k2' | 'ende_k3' | 'gymnasium' | ...), ADR 0010.
                                                        -- NULL = not recorded: the RW->PR check is skipped
                                                        -- with a warning, never guessed from the test date.
  assessment_note    TEXT,
  illegible_notes    TEXT,
  model_confidence   TEXT CHECK (model_confidence IN ('high','medium','low')),
  status             TEXT NOT NULL DEFAULT 'needs_review'
                     CHECK (status IN ('needs_review','validated_ok','validated_with_changes','rejected')),
  flags              TEXT NOT NULL DEFAULT '[]',      -- JSON array of flag codes
  review_notes       TEXT,                            -- human-resolvable detail per flag
  raw_response_json  TEXT,                            -- full validated payload, immutable after insert
  model_id           TEXT,
  runner              TEXT,                            -- 'agent_sdk' | 'direct'
  request_id         TEXT,
  created_at         TEXT NOT NULL DEFAULT (datetime('now')),
  opened_at          TEXT,                             -- stamped when a needs_review row is first selected
  edited_at          TEXT,                             -- stamped on first manual field edit; selects terminal state
  finalized_at       TEXT,                             -- stamped by finalize_assessment
  review_validation_json       TEXT,                   -- JSON array of {check, severity, message}
                                                        -- from demat.review_validation, or NULL if never run
  review_validation_at         TEXT,                   -- when the above was last computed
  review_validation_edited_at  TEXT                    -- snapshot of `edited_at` at that time, to detect staleness
                                                        -- (both NULL means "validated against the original import")
);
CREATE INDEX IF NOT EXISTS idx_assessments_person ON assessments(person_id);
CREATE INDEX IF NOT EXISTS idx_assessments_status ON assessments(status);
CREATE INDEX IF NOT EXISTS idx_assessments_inbox ON assessments(status, created_at DESC);

-- Overall/Gesamtwert-block scores, long-form: one row per emitted metric,
-- not fixed columns on `assessments`. See ADR 0005 §2.
CREATE TABLE IF NOT EXISTS assessment_metrics (
  assessment_id INTEGER NOT NULL REFERENCES assessments(assessment_id) ON DELETE CASCADE,
  position      INTEGER NOT NULL,        -- PK part, per ADR 0003's reasoning, NOT metric_key
  metric_key    TEXT NOT NULL,
  value_raw     TEXT,                    -- verbatim as printed: "20,5", "34", "≤1"
  value_num     REAL,                    -- coerced iff value_type is int/real
  value_text    TEXT,                    -- populated iff value_type is text
  confidence    TEXT CHECK (confidence IN ('high','medium','low')),
  value_provenance TEXT,                 -- 'sheet' | 'blank' | 'computed' | 'unavailable' | 'manual';
                                          -- NULL = legacy = 'sheet' (ADR 0013). Validated in Python,
                                          -- not a CHECK constraint, to stay additive (ADR 0010).
  PRIMARY KEY (assessment_id, position)
);
CREATE INDEX IF NOT EXISTS idx_assessment_metrics_key ON assessment_metrics(metric_key, value_num);

CREATE TABLE IF NOT EXISTS subtest_scores (
  assessment_id INTEGER NOT NULL REFERENCES assessments(assessment_id) ON DELETE CASCADE,
  position      INTEGER NOT NULL,                      -- printed order
  subtest_key   TEXT NOT NULL,                          -- canonical key, resolved per ADR 0005 §9.2d
  printed_label TEXT,                                   -- what was actually emitted/printed
  name          TEXT,
  value_raw     TEXT,                                   -- verbatim as printed: "3,5"
  rw            REAL,
  rw_max        REAL,
  pr            INTEGER,
  confidence    TEXT,
  pr_provenance TEXT,                                   -- same vocabulary as
                                                          -- assessment_metrics.value_provenance (ADR 0013)
  rw_provenance TEXT,                                   -- 'sheet'|'blank'|'computed'|'unavailable'|'manual';
                                                          -- NULL = legacy = 'sheet' (no backfill). Only
                                                          -- sum-target subtests (ADR 0015) are ever
                                                          -- 'blank'/'computed'. Validated in Python.
  PRIMARY KEY (assessment_id, position)                -- NOT subtest_key: see ADR 0003
);
CREATE INDEX IF NOT EXISTS idx_subtest_key ON subtest_scores(subtest_key);

-- Test-definition catalog: a queryable projection of demat/test_definitions/*.toml,
-- kept in sync by demat/test_registry.py:sync_to_db() at startup. Replaces
-- subtest_catalog (ADR 0005 §1, §3).
CREATE TABLE IF NOT EXISTS test_definitions (
  test_version      TEXT PRIMARY KEY,
  test_key          TEXT NOT NULL UNIQUE,  -- 'demat2plus'
  display_name      TEXT NOT NULL,
  extraction_skill  TEXT NOT NULL,
  aliases_json      TEXT NOT NULL DEFAULT '[]',
  norm_test         TEXT,                  -- external KB data/<test>/ dir, or NULL
  definition_file   TEXT,
  definition_sha256 TEXT,
  spec_json         TEXT NOT NULL,         -- the whole parsed definition, for provenance
  is_active         INTEGER NOT NULL DEFAULT 1,
  loaded_at         TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS test_definition_metrics (
  test_version TEXT NOT NULL REFERENCES test_definitions(test_version) ON DELETE CASCADE,
  metric_key   TEXT NOT NULL,
  label        TEXT NOT NULL,
  value_type   TEXT NOT NULL CHECK (value_type IN ('int','real','text')),
  unit         TEXT,
  min_value    REAL,
  max_value    REAL,
  max_possible REAL,                      -- canonical maximum, e.g. rw_total_max
  required     INTEGER NOT NULL DEFAULT 0,
  norm_scale   TEXT,                      -- KB `skala` key, e.g. 'gesamtwert'
  norm_field   TEXT,                      -- KB normen.csv column: pr | pr_band | t_wert | t_band
  position     INTEGER NOT NULL,
  PRIMARY KEY (test_version, metric_key)
);

CREATE TABLE IF NOT EXISTS test_definition_subtests (
  test_version  TEXT NOT NULL REFERENCES test_definitions(test_version) ON DELETE CASCADE,
  subtest_key   TEXT NOT NULL,
  printed_label TEXT,
  name          TEXT NOT NULL,
  rw_max        REAL,
  value_type    TEXT NOT NULL DEFAULT 'real' CHECK (value_type IN ('int','real')),
  norm_scale    TEXT,                     -- KB `skala` key, e.g. 'subtest_ZE'
  position      INTEGER NOT NULL,
  PRIMARY KEY (test_version, subtest_key)
);

-- Classroom Profile reports (ADR 0008): file on disk + DB row with the path,
-- the ADR 0007 storage precedent. Additive only, no migration (spec §7):
-- no FK to `assessments` (the id list is a historical record of what the
-- profile was built from; deleting an assessment must never mutate an
-- issued report), and append-only (regenerating inserts a new row; old
-- markdown files stay on disk).
CREATE TABLE IF NOT EXISTS class_profiles (
  profile_id          INTEGER PRIMARY KEY AUTOINCREMENT,
  klasse              TEXT NOT NULL,          -- uppercased, as stored on assessments
  schuljahr           TEXT NOT NULL,          -- derived label, e.g. '2026-27'
  test_version        TEXT NOT NULL,
  test_form_summary   TEXT,                   -- 'A' | 'B' | 'A/B'
  test_date_min       TEXT,                   -- ISO
  test_date_max       TEXT,                   -- ISO; equal to min for a single-sitting class
  norm_group          TEXT,                   -- DERIVED (spec §6.2.1), e.g. 'k3_gesamt'
  classification_basis TEXT,                  -- 'pr' | 't' (spec §6.2.2), as used for the buckets
  child_count         INTEGER NOT NULL,
  assessment_ids_json TEXT NOT NULL,          -- JSON array of the VALIDATED ids the profile was built from
  content_hash        TEXT,                   -- sha256 over sorted (id, edited_at, finalized_at); optional
  markdown_path       TEXT NOT NULL,          -- data/reports/<slug>/<ts>.md
  pdf_path            TEXT,                   -- always NULL this iteration; reserved for a later follow-up
  prose_run_dir       TEXT,                   -- data/runs/<profile_run_id>/, provenance
  model_id            TEXT,
  runner              TEXT,
  generated_at        TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_class_profiles_key
  ON class_profiles(klasse, schuljahr, test_version, generated_at DESC);

-- Child reports (ADR 0014): one printable page per validated assessment.
-- Same storage precedent as class_profiles: file on disk + DB row with the
-- path, additive only, append-only (regenerating inserts a new row; old
-- markdown files stay on disk). No FK to assessments, for the same reason
-- as class_profiles — an issued report must stay retrievable exactly as
-- issued even if the assessment is later deleted.
CREATE TABLE IF NOT EXISTS child_reports (
  report_id       INTEGER PRIMARY KEY AUTOINCREMENT,
  assessment_id   INTEGER NOT NULL,       -- historical reference only, no FK
  person_ref      TEXT NOT NULL,          -- person_id, or 'unlinked-<assessment_id>'
  test_version    TEXT NOT NULL,
  content_hash    TEXT,                   -- sha256 over (assessment_id, edited_at, finalized_at)
  markdown_path   TEXT NOT NULL,          -- data/reports/children/<person_ref>/<ts>.md
  template_version TEXT NOT NULL,
  generated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_child_reports_key
  ON child_reports(assessment_id, generated_at DESC);
CREATE INDEX IF NOT EXISTS idx_child_reports_person
  ON child_reports(person_ref, generated_at DESC);
