# ADR 0010 — The norm-table variant is an asserted property of each assessment

- Status: Proposed
- Date: 2026-09-20
- Supersedes in part: ADR 0008 (the "norm group is derived from the class key, never chosen" decision)

## Context

Every DEMAT result is read off one printed norm table, and each test publishes several. The knowledge
base enumerates them as `normgruppe` values in `data/<norm_test>/normen.csv`, described in
`tabellen.json`. Verified against the KB at `/Users/agherasim/Projects/demat-knowledge-base`
(`scripts/lookup.py list` reads `normen.csv` only; the row counts below account for every data row in
each file, so these lists are exhaustive):

| `norm_test` | `normgruppe` values (gender suffix stripped) | gender split | `beschreibung` from `tabellen.json` |
| --- | --- | --- | --- |
| `demat1plus` | `k1`, `k2` | yes (`_gesamt` / `_mädchen` / `_jungen`) | „1. Klasse, letzter Schuljahresmonat" / „2. Klasse, erste drei Schuljahresmonate" |
| `demat2plus` | `k2`, `k3` | yes | „2. Klasse, letzter Schuljahresmonat" / „3. Klasse, erste drei Schuljahresmonate" |
| `demat3plus` | `ende_k3`, `anfang_k4` | no | „Ende Klassenstufe 3" / „Anfang Klassenstufe 4" |
| `demat5plus` | `übergreifend`, `gymnasium`, `realschule`, `hauptschule`, `sekundarschule` | no | „Schulformübergreifend" / „Gymnasium" / „Realschule" / „Hauptschule" / „Sekundarschule" |

These are one kind of thing: **which published table the scorer used**. DEMAT 3+ happens to name its
two entries after points in the school year; DEMAT 5+ names its five after school types. That is a
difference in naming, not in kind.

The current code treats the first three as a *derived timepoint* and the fourth as an *exception*.
`demat/school_year.py::timepoint()` infers „anfang" vs „ende" from the calendar month of
`test_date_iso` (Aug–Dez ⇒ anfang, Jan–Jul ⇒ ende — the August Schuljahr cutover, read a second way),
and `demat/reports/norms.py::resolve_norm_group()` maps that guess through `_TIMEPOINT_RESOLVER` to a
group name, special-casing `demat5plus` to `übergreifend` unconditionally. The result decides which
table `demat/review_validation.py::_check_norm_mapping` re-derives every stored PR/T from — the one
place RW→PR correctness is enforced as a hard error.

Three things are wrong with that:

1. **It guesses a fact.** Which table the scorer used is not a function of the test month. A class
   tested in the first week of August was still measured against the table for the year that ended;
   a school testing outside the usual windows breaks the cutover entirely. When the guess is wrong,
   `_check_norm_mapping` compares every value against the wrong table and produces a wall of false
   hard errors — or silently confirms values that came from a different table than the one assumed.
2. **DEMAT 5+ is not actually an exception**, and treating it as one means its five school-type
   tables are unreachable: the app hardcodes `übergreifend` and can never check a sheet scored against
   the Gymnasium table.
3. **The group→label table is already ambiguous, and already wrong.** `_NORM_GROUP_LABELS` is keyed by
   group name alone, so `k2_gesamt` gets one label across all tests — „Ende 2. Klasse". That is right
   for DEMAT 2+ and wrong for DEMAT 1+, where the KB describes `k2_gesamt` as „2. Klasse, erste drei
   Schuljahresmonate", i.e. *Anfang* der 2. Klasse. A group name only means something in the context
   of its test.

The project owner's instruction is that this is one explicit property, chosen at upload from the
published variants, uniform across all four versions.

This changes ADR 0008, which decided the norm group is "derived from the class key, not chosen … no
per-report selector, no override UI", and rejected a user-selected comparison norm as "a choice that
has exactly one correct answer". That holds only if the correct answer is recoverable from stored
data. It is not: the table the scorer used was never recorded, and the August cutover is a proxy for
it, not a computation of it. ADR 0008's neighbouring decision — that `schuljahr` must not be stored
because it would drift from `test_date_iso` — is untouched: Schuljahr remains a pure function of the
date; the norm variant never was one.

The detailed implementation spec for this change is maintained outside version control; this ADR is
the durable record.

## Decision

**`assessments.norm_variant` (TEXT, nullable) stores the published norm-table variant, gender suffix
stripped.** Values are exactly the KB's `normgruppe` values with `_gesamt`/`_mädchen`/`_jungen`
removed — `k1`, `k2`, `k3`, `ende_k3`, `anfang_k4`, `übergreifend`, `gymnasium`, `realschule`,
`hauptschule`, `sekundarschule`. Gender is already a separate stored field and is composed back in at
lookup time, exactly as today. One additive nullable TEXT column, so an existing database upgrades in
place with no migration script.

**Nothing about norm resolution is derived from `test_date_iso` any more.**
`resolve_norm_group(norm_test, norm_variant)` takes the stored variant and no date. There is no
fallback, no heuristic and therefore no need to record where the value came from — the reason this
ADR has no `*_source` companion column. `school_year.timepoint()` and its tests are deleted; the
August cutover survives in `school_year.school_year()`, which keeps its Schuljahr role. `test_date_iso`
keeps every other job it has (Schuljahr label, class key, duplicate detection, Testzeitraum, display)
and loses only this one.

**The variant is chosen at upload, per test version, and corrected per assessment in review.** Page
triage decides each sheet's version, so a single control for a batch cannot be right; the Load
Assessments page shows one selectbox per registered test version, each defaulting to „— (nicht
angegeben)" (NULL). The pipeline stamps each saved assessment with the variant chosen for the version
that sheet turned out to be. The Assessment Review edit form offers the same per-assessment
selectbox, alongside `klasse`/`test_date_iso`, which are already reviewer-editable (ADR 0007). The
stored value is never recomputed afterwards by anything.

**The variant list lives in a KB-free static table in this repo, guarded by a consistency test.**
`demat/norm_variants.py` holds, per `norm_test`, the ordered variants with their German labels (taken
from the KB's `tabellen.json` `beschreibung`) and whether that test has gender-split groups. The
deciding constraint is availability, not staleness: `config.KNOWLEDGE_BASE_DIR` is legitimately absent
on a fresh checkout (ADR 0005 §9.6), and an upload dropdown that cannot render without the KB would
make the app unusable exactly where ADR 0005 says it must degrade gracefully — shelling out to
`lookup.py list` on every page render would also be slow and would put a subprocess in a Streamlit
rerun path. Drift is handled where the repo already handles it: `tests/test_kb_consistency.py`
asserts the table matches the KB's distinct gender-stripped `normgruppe` values, and the gender-split
flag matches the presence of `_mädchen`/`_jungen` suffixes, skipping when the KB is absent. A future
KB drop that renames a variant or adds gender splits to DEMAT 3+ fails a test instead of silently
looking up a nonexistent group.

**Labels are keyed by `(norm_test, variant)`, not by group name.** This is what fixes the DEMAT 1+
mislabelling above. `_NORM_GROUP_LABELS` and `_TIMEPOINT_RESOLVER` are both replaced by the single
`norm_variants` table.

**A missing variant degrades the RW→PR check to a warning; it never blocks and never guesses.** With
`norm_variant` NULL — every pre-existing row, and any upload where the selector was left at „nicht
angegeben" — `_check_norm_mapping` emits a `warning` („Normtabelle nicht angegeben — RW→PR-Prüfung
übersprungen") and skips the per-value comparisons, the same shape as the existing
knowledge-base-unavailable downgrade (ADR 0005 §9.6). This is a real reduction in enforcement for
unset rows, and it is the honest one: the app cannot verify a mapping without knowing which table it
was read from, and inventing a table to check against is how a wrong Prozentrang gets confirmed.

**Existing rows are not backfilled.** They stay NULL and carry the warning until a human asserts a
value. A migration that stamped the old date heuristic into `norm_variant` would write the very guess
this ADR removes, and — with no source column, deliberately — nothing would afterwards distinguish a
guessed variant from an asserted one. If bulk correction proves necessary, the right shape is a bulk
*set* in the review UI, where a human asserts one value for a selected set of assessments.

**The class-profile path distinguishes "mixed" from "not recorded".** Over a class group's validated
rows: NULLs abstain, so one unrecorded sheet among twenty asserted ones does not discard the
assertion. Two *different* asserted variants refuse generation with an actionable message — the
per-child Prozentränge then come from two different tables and the distribution is not one
comparison, which is a numbers problem, not a label problem. All-NULL generates as before with the
header stating that the Normtabelle is not recorded (`norm_group` widens to `str | None`), because
nothing in the report path passes the group to a lookup — `classify()` and `class_norm()` take no
group and `norms.norm()` is not called from `demat/reports/*` — so the only consequence is an unnamed
label, and refusing there would kill the feature for every class that predates this change.

**`grade_cross_check_warning()` keeps its arity and its advisory-only contract, and now cross-checks
the variant.** The expected grade is parsed from the variant itself (`(?:ende_|anfang_)?k(\d+)`), so
`anfang_k4` against a `klasse` of „3c" warns, and DEMAT 5+'s school-type variants match no pattern and
are skipped. It catches something more useful than before: a mis-selected dropdown entry is a likelier
error than a mistyped date, and the check is now also surfaced at review time, not only in the report.

**`ExtractedSheet` and the extraction skills are untouched.** No skill is asked to read a norm table
off a sheet. Three of the four version skills are stubs against unverified layouts, `reference.md` is
copied verbatim into every skill directory by `scripts/sync_skills.py` (so any field addition is an
all-skills change), and the value decides which norm table applies — it stays a human assertion, not
a model output, consistent with ADR 0008's "Python computes, the agent judges" split.

**Validation is in Python, not a `CHECK` constraint.** `store._add_missing_columns` can only
`ALTER TABLE ... ADD COLUMN ... TEXT`, so a constraint in `schema.sql` would give a fresh database a
guarantee an upgraded one lacks. Both writers validate the value against the variant table for that
row's `norm_test`.

## Alternatives rejected

- **Keeping the date-derived timepoint (status quo).** Guesses the one fact that decides which table
  every stored PR is checked against, and cannot reach DEMAT 5+'s four school-type tables at all.
- **Keeping the date derivation as a fallback under an explicit override** (the first draft of this
  ADR). Softer, and wrong: it keeps a guess in the resolution path, forces a `timepoint_source`
  column to tell guesses from assertions, and still can't express „Gymnasium". A value the app cannot
  verify should read as "not recorded", not as a confident default.
- **Storing the resolved `normgruppe` including the gender suffix.** Duplicates a field the row
  already has, and goes stale the moment a reviewer corrects `gender`. Storing the base variant and
  composing the suffix at lookup time keeps one fact in one place.
- **Storing the timepoint for 1+/2+/3+ and the school type for 5+ as two different properties.**
  Two columns, two code paths and two UI controls for one question — which printed table was used.
- **Calling `lookup.py list --test <T>` to populate the dropdown.** The authoritative source, and
  unusable at the point of need: the KB is legitimately absent (ADR 0005 §9.6), and the call would sit
  in a Streamlit rerun path. A static table plus a consistency test gets the correctness without the
  availability coupling.
- **Reading `data/<norm_test>/tabellen.json` directly from the app.** Bypasses `lookup.py`, which
  ADR 0008 makes the only KB entry point, and still requires the KB to be present.
- **Extending the KB's `lookup.py list` to emit `beschreibung`.** A change to a separate, versioned
  repository, for labels that are four short German strings per test.
- **A backfill script stamping the old date heuristic onto existing rows.** Convenient, and it writes
  exactly the guess this ADR exists to remove — permanently indistinguishable from an assertion, since
  there is deliberately no source column. Legacy rows read as "not recorded", which is true.
  Bulk-setting from the review UI, where a human asserts the value, is the acceptable shape if this
  becomes painful.
- **Blocking class-profile generation when no variant is recorded.** Would make every class that
  predates this change ungeneratable for the sake of a header label the report can simply state as
  unknown. Mixed asserted variants are different and do block: that is two norm tables in one
  distribution.
- **Making `norm_variant` part of the class grouping key.** A class sits one test scored against one
  table; a mix is a data error to surface, not a second cohort to invent — the same reason ADR 0008
  excluded `test_form`.
- **A single global variant selector for an upload batch.** Triage decides each page's version after
  the upload, so one control cannot name a valid variant for a mixed batch; per-version selectboxes
  can, and collapse to a single visible choice in the common single-version case.
- **Requiring the variant before extraction may proceed.** Would block ingesting a stack of sheets
  whose norm table the uploader has to go and look up, and the review screen is where every other
  header field is already corrected. Unset is a visible warning, not a silent default.
- **A `default_norm_variant` in the test-definition TOML** (ADR 0008's `default_norm_group`, renamed).
  Still a per-sitting fact dressed as a per-version constant: DEMAT 2+ legitimately uses `k2` or `k3`,
  and DEMAT 5+ any of five school types, for the same version.
- **A per-report norm-group selector on the Class Profile page** (the shape ADR 0008 rejected). Still
  rejected. The variant is an attribute of the assessments, captured where they enter the system; a
  report-time selector would let two runs over the same data disagree with nothing recorded about why.
