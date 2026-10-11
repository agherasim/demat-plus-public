# ADR 0005 — Data-driven test definitions: overall scores as rows, not columns

- Status: Accepted
- Date: 2026-09-20
- Extends: ADR 0003, ADR 0004
- Supersedes: the `subtest_catalog` table introduced in ADR 0003
- §9 verified against the knowledge-base drop of 2026-09-20 (German-language data interface)

## Context

ADR 0003 got half of this right. Subtests are already rows (`subtest_scores`) with a
per-version catalog (`subtest_catalog`), so a new DEMAT version needs no schema change for
its subtest list. The **overall block did not get the same treatment**: `rw_total`,
`rw_total_max`, `pr_total`, `pr_band`, `t_total`, `t_band` are literal columns on
`assessments`, mirrored as fixed fields on `OverallScores` in `demat/schema.py`, mirrored
again as fixed checks in `demat/validation.py` (`total_mismatch`, `pr_out_of_range`,
`t_out_of_range`), and mirrored a fourth time in the `display_columns` list in `app.py`.

That is the Hogrefe DEMAT summary shape — Rohwert / Prozentrang / T-Wert — hardcoded in four
places. ADR 0004 already noted that the DEMAT versions "differ in … the composition of
the Gesamtwert block", so the shape is not even stable within DEMAT. §9 below, written after
reading the DEMAT knowledge base at `data/demat-knowledge-base/`, shows just how unstable:
DEMAT 2+ has ten small subtests, DEMAT 3+ has three subtests over nine task types *plus* a
separate class-level norm system, DEMAT 5+ has two normed subtests and one that the manual
declines to norm at all. The stated requirement is stronger still: define *new test types* in
future, potentially entirely different instruments, **without a schema migration or a change
to the core storage layer each time**.

A second gap: `subtest_catalog` describes a version's subtests but nothing else about it —
not its display name, not which extraction skill handles it, not its validation bounds. The
version → skill mapping lives in a Python dict (`extraction.SKILL_FOR_VERSION`), the version
list lives again in the triage `SKILL.md`, and again in the `app.py` selectbox. Adding a test
type today means editing five files in three languages.

## Decision

### 1. A test definition is an authored data file; the DB catalog is a projection of it

Test definitions are authored as **one TOML file per test type** under
`demat/test_definitions/<test_key>.toml`, read at startup by a new `demat/test_registry.py`,
validated against a Pydantic `TestDefinition` model, and **upserted** into catalog tables.

TOML, not YAML or JSON: `tomllib` is stdlib on Python 3.13 (this project's floor) so it adds
no dependency, it is read-only which is exactly the access we need, and unlike JSON it takes
comments — these files carry provenance annotations ("max aus KB `tabellen.json`, Manual S. 43").

```toml
test_key         = "demat2plus"          # slug; filename stem. Conforms to the external KB's naming (§9.5)
test_version     = "DEMAT 2+"            # printed label; matches triage output and assessments.test_version
display_name     = "DEMAT 2+ — Deutscher Mathematiktest für zweite Klassen"
extraction_skill = "demat-2plus-extraction"
aliases          = ["DEMAT 2+", "DEMAT2+", "DEMAT 2plus"]   # triage-output normalisation
norm_test        = "demat2plus"          # external KB data/<test>/ dir; null if no norm data

[[overall_metrics]]
key = "rw_total"; label = "Rohwert gesamt"; value_type = "real"   # 0.5 steps — see §9.3
min_value = 0
# max_possible verified two ways: the KB norm series for skala=gesamtwert ends at rohwert 36.0
# (data/demat2plus/normen.csv), and the ten tabellen.json subtest maxima sum to 36. They agree.
max_possible = 36
required = true; norm_scale = "gesamtwert"

[[overall_metrics]]
key = "pr_total"; label = "Prozentrang gesamt"; value_type = "int"
min_value = 0; max_value = 100; norm_scale = "gesamtwert"; norm_field = "pr"

[[overall_metrics]]
key = "pr_band";  label = "Prozentrang-Bereich"; value_type = "text"
norm_scale = "gesamtwert"; norm_field = "pr_band"

[[overall_metrics]]
key = "t_total";  label = "T-Wert"; value_type = "real"
min_value = 20; max_value = 80; norm_scale = "gesamtwert"; norm_field = "t_wert"

[[overall_metrics]]
key = "t_band";   label = "T-Wert-Bereich"; value_type = "text"
norm_scale = "gesamtwert"; norm_field = "t_band"

[[subtests]]
key = "ZE"                    # canonical key; matches the KB's skala suffix (see §9.2)
printed_label = "ZE"          # what is printed on the sheet — may differ, e.g. LÄ vs key LA
name = "Zahleneigenschaften"
rw_max = 2                    # KB tabellen.json meta "max=2", Manual S. 43
value_type = "int"
norm_scale = "subtest_ZE"

# ... LA/LÄ, AD, SU, DO, DI, HA, GEL, SA (value_type "real", halbe Punkte), GEO

[[rules.sum]]                      # drives the total_mismatch flag, per test type
metric = "rw_total"
over   = "subtests.rw"
```

`position` is implicit in file order for both `overall_metrics` and `subtests`; the loader
assigns it. Hand-maintaining position numbers is a diff hazard for no gain.

**Definitions must load without the knowledge base present.** `norm_test`, `norm_scale` and
`norm_field` are advisory join hints to an *external, swappable* dependency (§9.6). The
registry never reads the KB to parse or validate a definition; a missing or refreshed KB must
never stop the app from starting or a sheet from being extracted.

**The loader UPSERTs.** `schema.sql` seeds `subtest_catalog` today with `INSERT OR IGNORE`,
which means editing a seed row silently does nothing after the first run — precisely the
failure that would make "just edit the file" feel broken. The registry instead, per
`test_version` and inside one transaction: `ON CONFLICT DO UPDATE` on `test_definitions`,
then `DELETE` + re-`INSERT` the child metric/subtest rows. Definition files are the source of
truth; the tables are a queryable cache of them. A definition that disappears from disk is
left in the DB (historical assessments still reference its keys) and marked inactive.

### 2. Storage: hybrid-lite — test-agnostic columns stay, the score block goes long-form

`assessments` keeps every column that is **true of any assessment of any instrument**:
person, source file/page, image path, test_version/form, extraction_skill, klasse, gender,
dates, assessment_note, illegible_notes, review/flags/notes, and the provenance block.

The six DEMAT-shaped score columns are **dropped** and replaced by a child table:

```sql
CREATE TABLE assessment_metrics (
  assessment_id INTEGER NOT NULL REFERENCES assessments(assessment_id) ON DELETE CASCADE,
  position      INTEGER NOT NULL,        -- PK part, per ADR 0003's reasoning, NOT metric_key
  metric_key    TEXT NOT NULL,
  value_raw     TEXT,                    -- verbatim as printed: "8-16", "20,5", "34"
  value_num     REAL,                    -- coerced iff value_type is int/real
  value_text    TEXT,                    -- populated iff value_type is text
  confidence    TEXT CHECK (confidence IN ('high','medium','low')),
  PRIMARY KEY (assessment_id, position)
);
CREATE INDEX idx_assessment_metrics_key ON assessment_metrics(metric_key, value_num);
```

`PRIMARY KEY (assessment_id, position)` rather than `(assessment_id, metric_key)` is the same
call ADR 0003 made for `subtest_scores`: a model that emits `pr_total` twice must produce a
`duplicate_metric` flag, not an `IntegrityError` that aborts the save mid-transaction and
drops the whole record.

**Three value columns, not one.** A single `metric_value TEXT` would lose SQL-side numeric
filtering and sorting; a single `REAL` cannot hold `"8-16"`. `value_raw` additionally keeps the
sheet's literal text, which this app cares about more than most — the never-auto-correct rule
(ADR 0003) means the printed string is evidence, and storing it makes coercion failures
recoverable without a re-run. `value_num` is `REAL`, not `INTEGER`, because DEMAT raw scores
are not integers (§9.3).

**Why not keep the DEMAT columns and add an overflow table.** Two storage locations for one
conceptual field set means every read path needs a `COALESCE` between "column if DEMAT, row
if not", and the privileged shape is DEMAT's — which §9 shows is not uniform across DEMAT
1+/2+/3+/5+. The privilege would have to be re-litigated on the first non-DEMAT instrument
anyway. Rejected as a migration deferred, not avoided.

**Why the split is worth keeping at all** (i.e. not pushing klasse/date/note into metrics
too): person, source provenance and review state are the columns the UI filters, joins and
indexes on for *every* test type. Generalising those buys nothing and costs every query.

**Cost accepted:** the Results table needs a pivot. SQLite cannot pivot dynamically, so
`store.list_assessments` pivots in Python using the registry's metric keys for the filtered
`test_version` (and, with no version filter, the union of keys across returned rows). At this
app's scale — one school's sheets, thousands of rows at the outside — that is a non-issue.

### 3. `subtest_catalog` → `test_definitions` + `test_definition_metrics` + `test_definition_subtests`

```sql
CREATE TABLE test_definitions (
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

CREATE TABLE test_definition_metrics (
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

CREATE TABLE test_definition_subtests (
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
```

`assessments.test_version` deliberately carries **no foreign key** to `test_definitions`. An
unrecognised sheet must still be storable via `demat-generic-extraction` with an
`unknown_test_version` flag (ADR 0004); an FK would turn that into a save failure and break
the never-drop-a-record rule.

`rw_total_max` illustrates a boundary worth stating: the *canonical* maximum for a test type
lives in the definition (`max_possible`), never in `assessment_metrics`. Only a maximum
actually printed on that particular sheet would be stored per-assessment — and DEMAT 2+ does
not print one, so in practice `rw_total_max` disappears as a stored field entirely.

### 4. A new test type = exactly two authored files

1. `demat/test_definitions/<test_key>.toml` — declares `test_version`, metrics, subtests,
   rules, and **which skill extracts it** (`extraction_skill`).
2. `demat/skills/<that-skill>/SKILL.md` — how to read *that* sheet: layout quirks, where the
   fields sit, what the handwriting tends to do.

Nothing else. The Python dict `extraction.SKILL_FOR_VERSION` is deleted in favour of a
registry lookup; the `app.py` version selectbox is populated from the registry; the triage
skill's list of known versions is generated from the registry by the existing
`scripts/sync_skills.py` (extended), covered by the existing drift test.

**Who owns the shape, definition or skill.** ADR 0004 said the skill markdown is the single
source of truth for extraction instructions. This ADR narrows that: the **definition owns the
data shape** (which metric keys and subtest keys to emit), the **skill owns the reading
technique**. The orchestrator injects the definition's expected key list into the prompt at
call time as a new `context` argument on the `SkillRunner` protocol — verified present and
plumbed through both `ClaudeAgentSdkRunner` and `DirectMessagesRunner`, though today only
under the retry-specific `extra_instructions` wording, hence a separate parameter rather than
reuse. Runtime injection beats ADR 0004's generate-a-file-and-test-for-drift pattern here
because there is no generated artifact to drift: the definition is read on the same call that
uses it. Skill files therefore lose their duplicated subtest tables and get shorter.

### 5. Extraction contract: every value is a verbatim string; typing happens at the storage boundary

`OverallScores` (fixed fields) is replaced on `ExtractedSheet` by:

```python
class MetricValue(BaseModel):
    key: str  # matches a definition metric_key
    value: str | None = None  # verbatim as printed; null if blank/illegible
    confidence: Confidence


overall: list[MetricValue] = []
```

Every extracted value — overall metrics **and** `SubtestScore.rw` / `rw_max` — is a
**string in the Pydantic extraction contract** (`rw: str | None`, was `int | None`), and is
coerced to a typed value in `store.py` per the definition's `value_type`, with a
`metric_unparseable` flag (never a save failure) on coercion failure. Coercion accepts the
German decimal comma.

To be unambiguous about the two layers, because they change in different directions:

| | Pydantic model field | SQLite column |
|---|---|---|
| overall metric | `MetricValue.value: str \| None` | `assessment_metrics.value_raw` TEXT + `value_num` REAL / `value_text` TEXT |
| subtest score | `SubtestScore.rw: str \| None`, `rw_max: str \| None` | `subtest_scores.value_raw` TEXT + `rw`, `rw_max` **REAL** (were INTEGER) |

Reasons for the string contract: a `int | float | str | None` union is a poor structured-output
schema for `DirectMessagesRunner`'s `client.messages.parse`; "transcribe exactly what is
printed" is this app's core instruction and a typed field invites the model to normalise
`"20,5"` to `20` or `205` silently; and the definition already knows the type, so the model
does not need to decide. The `REAL` columns are the §9.3 bug fix and are independent of the
string contract — both are needed.

`SubtestScore.abbreviation` is renamed `key`.

### 6. Validation becomes definition-driven

`validate()` takes the `TestDefinition` (or `None`) instead of the abbreviation→row dict.
Rules that were DEMAT-specific become generic:

| Today (hardcoded) | Becomes |
|---|---|
| `total_mismatch` vs `overall.rw_total` | driven by `[[rules.sum]]`; no rule → no check |
| `pr_out_of_range` (0–100) | `min_value`/`max_value` on any metric → `metric_out_of_range` |
| `t_out_of_range` (20–80) | same rule |
| `unknown_subtest` | unchanged in spirit; keys now from `test_definition_subtests` |
| — (new) | `unknown_metric`, `missing_required_metric`, `duplicate_metric`, `metric_unparseable` |

Test-agnostic checks (name present, confidence levels, date plausibility, duplicate subtests,
`rw_exceeds_max`) are unchanged. With no definition (generic fallback path), definition-driven
checks are skipped and `unknown_test_version` still fires, as today. The `[[rules.sum]]`
comparison must use a tolerance (`abs(sum - total) > 0.01`), not `!=`, now that raw scores are
floats.

### 7. Migration: drop and recreate

There is no production data yet. The change is applied by **editing `schema.sql` in place and
deleting `data/demat.db`**. No migration framework, no `ALTER TABLE` ladder. This is
recorded explicitly rather than assumed: **if any real extracted data exists when this is
implemented, stop and write a migration script instead** — re-extraction costs model spend and
the source PDFs may no longer be at hand.

**Delete `data/demat.db` specifically — never `data/` wholesale.** The external knowledge base
lives inside that gitignored directory and stays there by decision (§9.6); a wholesale reset
destroys it.

### 8. Deferred: a Streamlit UI for authoring test definitions

Explicitly out of scope, explicitly not precluded. The file loader is the primary mechanism
because the paired artifact (a `SKILL.md`) is a file anyway, files diff and review, and the
user is technical. A future UI would write the same TOML files through the same Pydantic
model and call the same loader — the registry seam is what makes that additive.

---

## 9. Addendum — the DEMAT knowledge base as an external dependency

Verified against the knowledge base at `data/demat-knowledge-base/` as of the 2026-09-20 drop:
`SKILL.md`, `references/{allgemein,demat1plus,demat2plus,demat3plus,demat5plus}.md`,
`data/<test>/{normen.csv,tabellen.json,PARSE_BERICHT.md}`,
`data/demat3plus/{klassennormen_mittelwert,klassennormen_streuung}.csv`,
`scripts/{lookup.py,validate.py}`.

**Its status, decided by the user and load-bearing for everything in this section: it is an
external dependency, maintained in another repository and dropped in here.** We read it; we do
not own it, version it, restructure it, rename its files, or edit its scripts. Its contents are
not final — refreshed copies will be supplied. Every design choice below follows from that.

**This is not hypothetical.** Between the first draft of this ADR and its verification, a drop
renamed essentially the entire data interface: `norms.csv` → `normen.csv`, `tables.json` →
`tabellen.json`, `PARSE_REPORT.md` → `PARSE_BERICHT.md`, `class_norms_*.csv` →
`klassennormen_*.csv`, `common.md` → `allgemein.md`; every CSV/JSON field name to German
(`table→tabelle`, `norm_group→normgruppe`, `scale→skala`, `raw_score→rohwert`,
`t_value→t_wert`, `note→anmerkung`, `source_page→seite`, `not_available→nicht_verfügbar`,
`stats→kennwerte`, `description→beschreibung`); the status enum to German (`uncertain→unsicher`,
`blank_in_source→leer_im_original`, `source_anomaly→druckfehler_vermutet`); and the overall
scale key `total` → `gesamtwert`. The CLI verbs and flags (`list`/`norm`/`classify`/`class`,
`--test/--scale/--group/--raw/--allow-flagged`) were unchanged, but their *values* are now
German. Treat the layout as read-only, **not** as stable. §9.1's conditional test is the
tripwire that catches the next such drop.

### 9.1 `tabellen.json` is not a test definition — the concerns stay decoupled

`tabellen.json` is **norm-table metadata**: `tabelle` (which manual table), `normgruppe`,
`skala`, `beschreibung`, `meta`, sample statistics in `kennwerte` (N, M, SD, reliability, SE,
critical difference), `seite`, and a top-level `nicht_verfügbar` list. It is organised around
*what the manual prints*, not around *what a score sheet contains*. It has no notion of
`test_form` (A/B), no child-header fields, no extraction skill, no Gesamtwert block
composition, and no declaration that PR comes with a `pr_band` — that is a column in
`normen.csv`, not a declared field anywhere.

So: **two different concerns, kept decoupled.** The knowledge base answers "what does Rohwert
17 mean for this Normgruppe"; our `test_definitions` answer "what fields exist on this sheet
and how do we store and validate them". Deriving one from the other automatically would couple
our storage schema to an externally-maintained transcription's internal layout — which has
just been demonstrated to rename wholesale without notice — and would break on DEMAT 3+ where
`tabellen.json` mixes individual norms (A1–A8) with class norms (B1–B4) in one flat `tabellen`
array.

**But they must not drift on names and maxima**, which is what the join fields in §1 and §3
are for: `test_definitions.norm_test`, `test_definition_metrics.norm_scale`/`norm_field`,
`test_definition_subtests.norm_scale`. A test asserts that every non-null `norm_scale` appears
as a `skala` value in that test's `normen.csv`, and that every `rw_max` matches the `max=`
note in `tabellen.json` where one exists. Two mechanics that the drop made explicit:

- The `meta` entry is free text with a **German** suffix — `"max=4 (halbe Punkte möglich)"`,
  formerly `"max=4 (half points)"`. Parse the numeric value after `max=` and ignore the
  remainder; do not match the string.
- The test **skips — does not fail — when the knowledge base directory, or a specific
  `norm_test` subdirectory, is absent**, because the KB is untracked and a fresh checkout
  legitimately has none. When present and mismatched, it fails: a refreshed KB that renames a
  `skala` or corrects a maximum *should* turn the suite red so the TOML gets reconciled
  deliberately. Had this test existed before the 2026-09-20 drop, it would have caught
  `total → gesamtwert` immediately.

That is the anti-drift mechanism — shared keys plus a conditional consistency test, not a
derived artifact and not a script nobody runs.

### 9.2 Scale keys, and the one place umlauts must survive

The KB's `skala` keys are not uniform, because the manuals are not:

| Test | `skala` keys in the KB |
|---|---|
| DEMAT 2+ | `gesamtwert`, `subtest_ZE`, `subtest_LA`, `subtest_AD`, `subtest_SU`, `subtest_DO`, `subtest_DI`, `subtest_HA`, `subtest_GEL`, `subtest_SA`, `subtest_GEO` |
| DEMAT 3+ | `gesamtwert`, `subtest_arithmetik`, `subtest_sachrechnen`, `subtest_geometrie`; class norms use `ARI`, `SAR`, `GEO`, `GES` and task types `ZS AD SU MU SR SZ FL LS LU` |
| DEMAT 5+ | `gesamtwert`, `subtest_A` (Arithmetik), `subtest_C` (Sachrechnen); Subtest B (Geometrie) has **no norms at all** (listed in `nicht_verfügbar`) |

Four things follow.

**(a) `subtest_key` is the KB's `skala` suffix, and `printed_label` is what the sheet shows.**
Re-verified after the German rename: **the scale keys stayed ASCII** — it is still
`subtest_LA`, not `subtest_LÄ`, even though the surrounding data went German. The sheet prints
`LÄ`; today's `subtest_catalog` seed and `SKILL.md` both use `LÄ`. The definition therefore
carries `key = "LA"` (joining to `subtest_LA`) and `printed_label = "LÄ"`, and the extraction
contract asks for the **key**, with the printed label supplied in the injected prompt context
so the model can match what it sees to what it emits.

**(b) Norm-group identifiers, by contrast, are German and umlaut-bearing** — `k2_mädchen`,
`k3_mädchen`, `übergreifend` — and the KB's `SKILL.md` states the rule outright: *"never
transliterate them (`k3_mädchen`, not `k3_maedchen`)"*. These are lookup keys passed to
`--group`. They must be stored and transmitted verbatim, in UTF-8, with no normalisation.

**This creates a concrete hazard, because this codebase already contains a German-folding
function that would corrupt them.** `identity.normalize_name` maps `ä→ae` by design (ADR 0001,
where the folding is load-bearing for person-name matching). Applying it to a norm-group key
yields `k3_maedchen`, which matches nothing and fails the lookup. **Rule: `identity.py`'s
German folding is for person names only and must never be applied to any KB-facing
identifier.** The future `[[norm_groups]]` block (§9.4A) carries these strings verbatim.

**(c) The same hazard applies to the printed-label fold in (d) below** — and it invalidates a
rule an earlier draft of this ADR stated. That draft said `store.save_assessment` may match an
emitted subtest key to a definition key "after German folding". It cannot: German folding maps
`LÄ` → `lae`, which does not equal `la`. The correct fallback is **diacritic stripping**
(NFKD, drop combining marks), which maps `LÄ` → `LA`. Stated explicitly because the two
normalisations look interchangeable and are not.

**(d) `store.save_assessment` folds printed labels onto canonical keys.** When a definition is
in play, an emitted subtest key is resolved in this order: exact `key` match → exact
`printed_label` match → case-insensitive `key` match → diacritic-stripped match. The result is
stored as the canonical `key`; the emitted string is preserved in `printed_label`. Without
this, the same subtest would land as `LA` from the 2+ skill and `LÄ` from
`demat-generic-extraction` — two `subtest_key` values for one subtest, which silently breaks
exactly the cross-test comparison the history requirement exists to enable. The **generic path
stores what was printed and is not expected to join**: with no definition there is nothing to
fold onto, so those rows are best-effort transcription awaiting a definition, consistent with
their `unknown_test_version` flag. `unknown_subtest` matching accepts any of the four forms.

**(e) A subtest may legitimately have no norm.** DEMAT 5+ Subtest B is normed nowhere, by the
manual's own decision. `norm_scale` is therefore nullable and its absence is not a defect — it
is exactly what stops a future PR/T cross-check (§9.4) from inventing a comparison.

The nine DEMAT 3+ task types (`ZS`…`LU`) are a **third** level below subtests, used only for
class-level norms. This ADR does not model them: nothing in the current extraction pipeline
reads them off an individual score sheet, and class-level analysis is not in scope. Noted here
so that a later "Klassenprofil" feature knows the level exists and that `subtest_scores` is the
wrong table for it.

### 9.3 Correction: raw scores are not integers

The KB makes this unambiguous. `data/demat2plus/normen.csv` for `skala=gesamtwert` has
`rohwert` values `0.0, 0.5, 1.0, 1.5, …` up to `36.0`; `lookup.py` errors with *"DEMAT 2+
total/Sachaufgaben use 0.5 steps"*; and `tabellen.json` annotates DEMAT 2+ Sachaufgaben as
`max=4 (halbe Punkte möglich)`.

Today's `SubtestScore.rw: int | None`, `rw_max: int | None`, `OverallScores.rw_total: int`
and the `INTEGER` columns in `schema.sql` are therefore **wrong for the test we already
support**. A sheet with `SA = 3,5` and `RW gesamt = 20,5` either fails Pydantic validation and
burns the retry, or gets silently truncated. This is a live bug the knowledge base exposed,
and the correction is folded into this change: `REAL` columns, string model fields (§5),
`value_type = "real"` for `rw_total` and for the half-point subtests, and a tolerance-based
sum rule (§6).

`max_possible` for DEMAT 2+ `rw_total` is **36**, re-verified against the current drop two
independent ways that agree: the norm series for `skala=gesamtwert` terminates at
`rohwert 36.0`, and the ten per-subtest maxima in `tabellen.json` (ZE 2, LÄ 4, AD 4, SU 4,
DO 3, DI 4, HA 3, GEL 4, SA 4 halbe Punkte, GEO 4) sum to 36. Those per-subtest maxima also
finally fill in the `rw_max NULL where unverified` placeholders that today's `subtest_catalog`
seed and `SKILL.md` carry. Each value gets a TOML comment citing the `seite` from
`tabellen.json`. For the other tests, where the two sources are not both available, the norm
series is authoritative on the valid raw-score range — and a disagreement between the two is a
finding to report, not something to silently reconcile.

Because the KB is externally maintained, these numbers are **copied into the TOML, not read
from the KB at runtime**. A definition must parse and validate on a machine with no knowledge
base at all (§1); the consistency test (§9.1) is what catches a later divergence.

### 9.4 Two consumers of the norm data, and they want different mechanisms

Two distinct future features want this data, and conflating them would be a mistake.

**(A) Deterministic validation — `pr_mismatch` / `t_mismatch`. In-process, no agent.**
`normen.csv` carries exactly the four values a sheet prints (`pr`, `pr_band`, `t_wert`,
`t_band`) keyed by `(test, normgruppe, skala, rohwert)`. A handwritten PR that a teacher
looked up by hand and a vision model then transcribed has two independent error sources; the
norm table has neither. This belongs in `demat/validation.py` as a deterministic rule reading
the CSV through a small `demat/norms.py` reader — not an agent call. Paying a model to do a
table lookup is slow, non-deterministic, unbudgeted per sheet, and untestable.

**Still deferred, for one concrete reason: selecting the Normgruppe is not trivial.** DEMAT 2+
alone has six (`k2_gesamt`, `k2_mädchen`, `k2_jungen`, `k3_gesamt`, `k3_mädchen`,
`k3_jungen`), and choosing between `k2_*` and `k3_*` means deciding whether a test date falls
in "letzter Schuljahresmonat der 2. Klasse" or "erste drei Schuljahresmonate der 3. Klasse" — a
Bundesland-dependent school-calendar question. DEMAT 3+ splits `ende_k3` / `anfang_k4` the same
way. DEMAT 5+ groups by **school type** (`übergreifend`, `gymnasium`, `realschule`,
`hauptschule`, `sekundarschule`), which is not printed on the sheet at all. A wrong group
produces confident wrong flags on correct data, which is worse than no flag.

The seam lands now: `norm_test` on the definition, `norm_scale` / `norm_field` on metrics and
subtests, `KNOWLEDGE_BASE_DIR` in config, and `klasse` / `gender` / `test_date_iso` already
stored per assessment. A follow-up ADR decides the Normgruppe derivation rule (probably a
declarative `[[norm_groups]]` block with date-window and gender predicates, plus an explicit
"cannot determine" outcome) and the required-input question for DEMAT 5+. Three constraints to
carry into it:

- **Flag, never correct** (ADR 0003) — a computed PR is not more authoritative than the printed
  one; the teacher may have used a Normgruppe we guessed wrong.
- **Honour cell status**, whose values are now German: `ok` (use), `unsicher` (use, but a
  mismatch must not flag), `leer_im_original` (the manual prints no PR, so a blank on the sheet
  is *correct*), `druckfehler_vermutet` (probable misprint — must not flag; `lookup.py` refuses
  these outright without `--allow-flagged`). These are live in the data: DEMAT 2+ `k2_mädchen`
  `rohwert 35.5` is a `druckfehler_vermutet` cell.
- **Never transliterate Normgruppe identifiers** (§9.2b).

**(B) Interpretation — narrative assessment, Förderempfehlung, Klassenprofil. Agent skill.**
This is what the KB skill is built for: per-manual interpretation bands, class-norm deciles,
and rules about what a DEMAT result does and does not establish. It is genuinely a reasoning
task, runs per-child on demand rather than per-sheet, and reproducing it in Python would mean
reimplementing the manuals. This is the right consumer for `demat-knowledge-base` as a Skill.
Not in scope — but §9.7 wires the discovery so it is a small step rather than a refactor.

### 9.5 Version naming: the external KB's `test_key` naming is authoritative; DEMAT 4+ is removed

**Two key formats, one mapping, declared in the definition file.** `test_version` stays the
printed label (`"DEMAT 2+"`) because that is what triage reads off the sheet, what users
select in the UI, and what is already stored in `assessments.test_version`. `test_key` is the
slug (`"demat2plus"`), used as the definition filename stem. **`test_key` conforms to the
external KB's `demat1plus` / `demat2plus` / `demat3plus` / `demat5plus` naming** — unchanged
across the German rename, and already in use by a component we do not control, so our schema
adapts to it rather than the reverse. `norm_test` is kept as a separate field even though it
equals `test_key` today, so a future instrument can have a definition with no norm data
(`norm_test = null`) or point at a differently-named directory in a refreshed KB.

**DEMAT 4+ is removed from the codebase entirely.** The user has confirmed no DEMAT 4 sheets
exist in the corpus. The label was introduced symmetrically in ADR 0004 and never backed by a
sample sheet; the KB covers 1+/2+/3+/5+ and states "DEMAT 4 is **not** included". So:

- delete `demat/skills/demat-4plus-extraction/`;
- ship no `demat4plus` definition;
- remove `"DEMAT 4+"` from `extraction.SKILL_FOR_VERSION` (deleted wholesale anyway), the
  `app.py` selectbox (registry-driven anyway), and — easy to miss — **both** the frontmatter
  `description` and the body version list of `demat/skills/demat-sheet-triage/SKILL.md`;
- amend ADR 0004's skill table.

Should a DEMAT 4 sheet ever appear, it triages as an unknown version, routes to
`demat-generic-extraction` and flags `unknown_test_version` — strictly better than being
scored against a fabricated subtest table.

**DEMAT 5+ gains a real definition and a `demat-5plus-extraction` skill stub**, since the KB
has a full manual transcription for it and the pipeline currently has no route for it at all.
Once §4's generation step is in place, the triage skill's version list follows the registry
automatically and this class of drift stops recurring.

### 9.6 The knowledge base stays where it is, referenced through one configurable path

An earlier draft recommended relocating `data/demat-knowledge-base/` to a tracked
`knowledge_base/` directory, on the grounds that irreplaceable hand-transcribed data should not
sit untracked inside a directory used for disposable artifacts. **That recommendation is
withdrawn.** The user maintains the knowledge base in a separate repository and supplies
refreshed copies; it is a vendored external dependency, not project-owned content. Tracking it
here would create a second copy to diverge, and restructuring it would break the next drop-in.
It stays at `data/demat-knowledge-base/`, stays in `.gitignore` via the existing `data/` rule,
and its internal layout is treated as **read-only**: `scripts/lookup.py`,
`data/<test>/{normen.csv,tabellen.json}`, `references/*.md`. We do not rename, move or edit any
of it — including `lookup.py`'s `BASE`, which resolves relative to its own `__file__` and
therefore needs no change wherever the directory sits.

What this requires from our side is that the path appear **exactly once** in our code:

```python
# demat/config.py
KNOWLEDGE_BASE_DIR = Path(
    os.environ.get("DEMAT_KB_DIR", DATA_DIR / "demat-knowledge-base")
)
```

Everything else (the consistency test, the future `demat/norms.py`, the skill-discovery wiring
in §9.7) reads that one value. A refreshed copy is then a directory swap, and a copy kept
elsewhere entirely is one environment variable — neither is a code change.

Three consequences that must be honoured rather than assumed away:

- **Absent is a normal state, not an error.** Definitions parse, the registry loads, the
  pipeline extracts and the app starts with no knowledge base present. Only the optional
  consistency test (skipped) and the future norm cross-check (unavailable) depend on it.
- **Renamed is also a normal state.** The 2026-09-20 drop renamed every data file and field.
  Anything we write against the KB — the consistency test, the future `demat/norms.py` — should
  fail loudly with the offending file and key name, never fall back to a default or a guess.
- **The residual risk stays on the books.** The KB is still untracked and still inside `data/`,
  so `rm -rf data/` still destroys it. The mitigation is no longer relocation; it is that §7's
  migration deletes `data/demat.db` specifically and never the directory, and that this is
  stated wherever a reset is described.

### 9.7 Generalising skill discovery so external skills can be exposed

`config.ensure_skills_discoverable()` today creates a single symlink `.claude/skills` →
`demat/skills` (relative). That mechanism can expose exactly one directory, so an external
skill living outside the repository cannot be discovered at all.

**Change the mechanism now; defer the use.** `.claude/skills/` becomes a real directory
containing **one symlink per skill**:

```python
# demat/config.py
EXTERNAL_SKILL_DIRS: list[Path] = []  # e.g. [KNOWLEDGE_BASE_DIR] when §9.4(B) lands
```

`ensure_skills_discoverable()` ensures `.claude/skills/` is a directory (unlinking the old
single symlink if a previous version created one — the current early-return on `exists()`
would otherwise silently keep the old layout forever), then creates or refreshes
`.claude/skills/<name>` for every skill directory under `demat/skills/` **and** every path in
`EXTERNAL_SKILL_DIRS`, where `<name>` is the skill's frontmatter `name` (the KB's is
`demat-knowledge-base`). In-repo targets stay relative; external targets are **absolute**,
since they resolve from a configurable, machine-specific path. `.claude/skills/` is added to
`.gitignore` — it is generated, and one of its entries would encode a local path.

This is done now because it is cheap and because doing it later means touching the discovery
path a second time. Adding the knowledge base then becomes one list entry.

**Why exposing it is safe — and the one thing to verify first.** `ClaudeAgentSdkRunner` passes
`ClaudeAgentOptions(skills=[skill_name])`, i.e. names the single skill for each run, and sets
`allowed_tools=["Read","Write"]` with `disallowed_tools=["Bash", ...]`. On the assumption that
`skills=` is an allowlist scoping the session rather than an addition to whatever is
discovered, a `demat-knowledge-base` symlink cannot leak into an extraction run. **That
assumption must be confirmed before the symlink is actually added** — if `skills=` turns out to
be additive, wiring discovery changes the behaviour of every extraction call, and the entry
must be gated instead.

That tool scoping is also why §9.4(B) is deferred rather than merely unimplemented: the KB
skill requires every norm value to come from its lookup script, which needs **Bash** —
a tool ADR 0004 deliberately denies the extraction runner, on a path that handles scans of
children's score sheets. An interpretation pass therefore needs its own runner profile with
Bash scoped to the knowledge-base directory, and the extraction runner must keep Bash denied.
That is a security decision deserving its own ADR, not a quiet default. Wiring discovery now
costs nothing and prejudges none of it.

---

## Alternatives rejected

- **Everything in one generic `assessment_metrics` table, including klasse, gender, date,
  note.** Maximally uniform, but destroys the indexes and filters every read path uses and
  makes the Results table a pivot of a pivot. Generality nobody asked for.
- **Keep today's DEMAT columns, add a generic overflow table for future tests.** Smaller
  immediate diff, but institutionalises "DEMAT is the real shape, everything else is an
  exception", forces `COALESCE` into every query, and defers rather than avoids the migration.
- **JSON blob column `overall_scores_json` on `assessments`.** Zero new tables and SQLite has
  `json_extract`. But there is then no catalog to validate against, no cheap cross-assessment
  query on a metric ("all children with T-Wert below 40"), and the shape becomes whatever the
  model happened to emit. The `raw_response_json` column already covers the "keep everything
  verbatim" need; this would be that with extra steps.
- **One table per test type, generated from the definition (`demat_2plus_scores`, …).**
  Best query ergonomics per test, but "add a test type" becomes "run DDL at startup", which is
  exactly the schema-migration-per-test-type the requirement rules out.
- **Generate `test_definitions` directly from the knowledge base's `tabellen.json`.** Tempting —
  it removes the hand-authored duplicate — but `tabellen.json` describes manual tables, not
  score sheets (§9.1), has no room for `extraction_skill`, `aliases`, `test_form` or validation
  bounds, and is structurally inconsistent across tests. Decisively rejected once the KB became
  an external dependency, and doubly so after a drop renamed every field in it: our catalog
  would be generated from a directory that changes without our involvement and may be absent
  entirely.
- **Reading `rw_max` / `max_possible` from the KB at runtime instead of copying into the TOML.**
  Single source of truth, but makes an optional external dependency load-bearing for startup
  and for every validation run — and the German rename would have broken startup outright.
  Copy + conditional consistency test (§9.1) instead.
- **Relocating the knowledge base into a tracked directory** (§9.6). Protects irreplaceable
  data from an accidental `data/` wipe, but it is not ours to vendor: it is maintained
  elsewhere and re-supplied, so a tracked copy would diverge and a restructure would break the
  next drop-in. Rejected by the user's decision; the risk is instead mitigated by never
  deleting `data/` wholesale.
- **Use the KB's `skala` keys verbatim as our subtest keys, including the `subtest_` prefix.**
  Removes the mapping, but bakes an external naming convention into our primary keys and into
  every `SKILL.md`, and the KB's own keys are inconsistent between tests (§9.2). A short local
  key plus an explicit `norm_scale` join field is the smaller commitment — and the 2026-09-20
  rename of `total` → `gesamtwert` is the case in point: it cost one TOML line per definition
  instead of a data migration. (Note the asymmetry with `test_key`, where we *do* conform:
  `test_key` is a handful of values in a handful of files, while subtest keys would propagate
  into every stored row.)
- **Normalising KB identifiers through `identity.normalize_name`.** It is right there and it
  handles German — but it folds `ä→ae`, which turns `k3_mädchen` into a key that matches
  nothing, and `LÄ` into `lae` rather than `la`. Diacritic stripping for label matching; no
  normalisation at all for Normgruppe lookups (§9.2b–c).
- **Keeping `demat-4plus-extraction` as an inactive stub** rather than deleting it. Preserves
  the work if a DEMAT 4 sheet ever turns up, but the "work" is a placeholder table never
  validated against a real sheet, and a dormant skill invites someone to reactivate it.
  Deleted; the generic fallback covers the case correctly.
- **Shelling out to `scripts/lookup.py` per metric per sheet** for the deferred cross-check.
  Reuses the authoritative implementation, but it is a JSON-printing CLI that `sys.exit`s on
  failure, called from a Streamlit worker thread, turning error handling into exit-code
  archaeology. Read the CSV in-process and keep `lookup.py` as the reference implementation and
  the interactive tool it already is.
- **Invoking the `demat-knowledge-base` skill per sheet for PR/T validation.** Now technically
  available, but it would pay a model to do a table lookup, make a deterministic check
  non-deterministic, and require granting Bash to the extraction runner (§9.7). The skill is the
  right consumer for interpretation (§9.4B), not for validation.
- **YAML definition files.** Nicer to author than JSON, but needs `pyyaml`; `tomllib` is
  stdlib and comments are the only YAML feature these files actually need.
- **JSON definition files.** No dependency either, but no comments — and these definitions
  carry manual-page citations for every `rw_max` (§9.3).
- **Definitions authored only in the database, seeded by SQL.** No parser needed, but not
  diffable, not reviewable, and lost on the DB reset this very ADR performs.
- **Skills keep their own subtest tables, with a drift test against the definitions** (the
  ADR 0004 generated-`reference.md` pattern). Consistent with precedent, but that pattern
  exists because the agent must *read a file in its own directory*; the key list here can
  simply be in the prompt, and a generated artifact that can drift is worse than one that
  cannot exist.
- **Implement the PR/T norm cross-check now** (§9.4A). High value, but Normgruppe derivation is
  a school-calendar and school-type problem that would double the size of this change and can
  produce confident false flags if rushed. The join fields land now; the rule lands next.
