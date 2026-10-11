# ADR 0008 — Classroom Profile report generation

- Status: Proposed
- Date: 2026-09-20
- Superseded in part by: ADR 0010 (the norm group is now an asserted `norm_variant` property, not derived from the class key)

## Context

The app can extract and validate one child's DEMAT sheet at a time, and list every assessment in a
flat filterable table ("Results"). What it cannot do is answer the question a teacher actually
brings: *what does this class look like, and what should I do on Monday?* A hand-written sample
class-profile document (DEMAT 2+, class 3c, 24 children) sets the target shape — a class-distribution
summary, a per-subtest mean-PR chart, interpretive prose, a per-child section sorted by overall PR
with a ten-bar sparkline, and a closing interpretation note.

Naming convention for this feature: the feature, page, modules, files and table are named in English
(**Classroom Profile** / `class_profile` / `class_profiles`), consistent with the rest of the code
and UI chrome. Only the *generated report body* is German, and it keeps the manuals' terms verbatim
(Prozentrang, T-Wert, „unterdurchschnittlich") per the knowledge base's language convention.

Facts about the current code and data that shape this decision:

- **Numbers already exist and are already human-validated.** `subtest_scores.pr` and
  `assessment_metrics` carry the Prozentränge as transcribed off the sheet and confirmed by a
  reviewer on the Assessment Review page. Nothing about a class profile requires a model to produce
  a number.
- **The knowledge base forbids recalled norm values** (`SKILL.md` rule 2) and forbids a DEMAT result
  alone establishing a Rechenstörung/Dyskalkulie diagnosis (rule 6). Only `scripts/lookup.py` may
  produce a norm value, and class-level norms exist for DEMAT 3+ only.
- **Interpretation bands differ per version, and so does their basis.** `lookup.py`'s `BANDS` table
  classifies DEMAT 2+ and 3+ by Prozentrang (six categories each) and DEMAT 1+ and 5+ by T-Wert
  (five categories). The sample document's four PR buckets are a simplification of DEMAT 2+'s six.
- **No PDF renderer is installed.** `grep -iE "weasyprint|reportlab|playwright|pdfkit|xhtml2pdf|fpdf"
  uv.lock` is empty; `jinja2` is present only as a transitive Streamlit dependency.

The detailed implementation spec for this change is maintained outside version control; this ADR is
the durable record.

## Decision

**The Classroom Profile page replaces the "Results" nav page.** `NAV_PAGES` becomes
`["Load Assessments", "Assessment Review", "Class Profile"]`. **Assessment Review is untouched** —
it keeps its implementation, its `opened_at`/`edited_at` queue semantics, and both call sites of
`store.finalize_assessment()`, which remains the only writer of a terminal state (ADR 0007). The
profile therefore has a reliable supply of `validated_ok` / `validated_with_changes` rows, which are
the only rows it will read. Removing Results drops the cross-class filtered CSV export; the profile
page carries a per-class CSV export in its place, and the loss of the arbitrary cross-class export is
accepted rather than worked around.

**Deterministic numbers, LLM prose only.** Every Prozentrang, T-Wert, mean, median, count, bucket
membership and bar length in a report is computed in Python from `subtest_scores.pr` and
`assessment_metrics`. One LLM call drafts *only* the interpretive sections — „Was das Profil zeigt",
„Empfehlungen für den Unterricht", and the per-child Stärken/Schwächen/Empfehlungen — and receives
the computed numbers as structured input. This is enforced, not merely requested: the generated
prose is rejected unless every numeric token in it is a member of the allowed-number set derived
from the computed input, and unless it passes a diagnosis-term check. A prompt instruction is not an
enforcement mechanism; a validator is.

**The diagnosis rule is enforced as a referral carve-out, not a blanket ban.** `Rechenschwäche`,
`Rechenstörung`, `Dyskalkulie` and `Diagnose` may appear only inside a referral construction
(„Förderdiagnostik … erwägen", „weitere Abklärung", „abklären") — recommending further assessment is
not asserting a diagnosis, and the sample document does exactly this. The bare assertive form is
rejected. A blanket ban would either neuter the recommendations or, more likely, be switched off by
the first implementer who hits it.

**The knowledge base is called from Python, never by the agent.** `scripts/lookup.py` runs as a
subprocess from `demat/reports/norms.py`. The prose agent keeps the existing tool scoping
(`allowed_tools=["Read","Write"]`, `disallowed_tools=["Bash","WebFetch","WebSearch"]`), so it
physically cannot reach the KB and is never in a position to produce a norm value. KB rule 2 becomes
a property of the architecture rather than a request in a prompt. An absent `KNOWLEDGE_BASE_DIR`
stays a normal state (ADR 0005 §9.6): report generation is disabled with an explicit message,
never a crash and never a guessed band.

**Classification bands and their basis come from the manual, per version.** The bucket set is
whatever `lookup.py classify` returns for that version — six PR categories for DEMAT 2+ and 3+, five
T-Wert categories for DEMAT 1+ and 5+ — not the sample document's four, and never hardcoded. For a
version that classifies by T-Wert the distribution is built on the definition's `t_wert` metric, not
on Prozentränge. The per-subtest chart still plots Prozentränge where they exist, because that is
what the sheets carry.

**The comparison norm group is derived from the class key, not chosen.** Every child in a detected
group shares `(klasse, Schuljahr, test_version, test date window)` by construction, so there is no
comparison for a user to select and no per-report selector, no override UI, and no new field in the
test-definition files. The norm group is computed as `(norm_test, timepoint)`, where `timepoint` is
`anfang` for a test month of August–December and `ende` for January–July — the same August cutover,
for the same reason, as the existing Schuljahr derivation. This resolves to `k1_gesamt`/`k2_gesamt`
for DEMAT 1+, `k2_gesamt`/`k3_gesamt` for DEMAT 2+, and `ende_k3`/`anfang_k4` for DEMAT 3+. The grade
parsed from the `klasse` label is a **cross-check**, not an input: a disagreement between the label's
grade and the derived timepoint is reported in the profile, never silently resolved. Gender-split
norm groups (`_mädchen` / `_jungen`) are never used — a class is one mixed group, so `_gesamt` is the
only correct series. DEMAT 5+ is the one exception: its norm groups are school types
(`übergreifend`, `gymnasium`, `realschule`, `hauptschule`, `sekundarschule`), which are not a
function of the class key and are not stored anywhere; it uses `übergreifend`, printed in the header.

**Class norms are gated on `test_definitions.norm_test`, not on a version string.** Only
`norm_test == "demat3plus"` gets the Tabelle 5 class-mean/spread deciles (`lookup.py class`, which
structurally has no `--test` argument and whose `--scale` vocabulary is DEMAT 3+'s). For every other
version the per-subtest chart is labelled as a plain descriptive statistic over this class's own
children, with copy stating that no class norms are published for that version, and the PR-50 marker
captioned as the *norm sample's* midpoint. The report may not imply a comparison that does not
exist. This runs through the data-driven mechanism of ADR 0005 so adding a version with class norms
is a definition-file change, not a code change. `lookup.py class --scores` takes Rohwerte, not
Prozentränge.

**A class is `(upper(klasse), Schuljahr, test_version)`.** `test_form` is deliberately excluded —
A and B are parallel forms normed to the same tables, so a class that used both is one cohort; the
header renders „Form A/B (gemischt)". A group spanning several test dates stays one report, with the
header showing a Testzeitraum instead of a Testdatum and `class_profiles` recording both ends. Only
`validated_ok` / `validated_with_changes` assessments are eligible; the page and the report header
both state „N von M Bögen dieser Klasse sind noch nicht geprüft" rather than hiding the gap. An
assessment whose `klasse`, `test_version` or `test_date_iso` is NULL belongs to no class and is
surfaced as a count, never silently absorbed into a group.

**`_school_year` / `_klasse_schuljahre` move from `app.py` into `demat/school_year.py`.** Schuljahr
is derived from `test_date_iso` with an August cutover and is not stored; the report module must not
import the Streamlit script, and a second copy of the cutover rule would drift — especially now that
the norm-group derivation depends on the same cutover. The helper's today-fallback for a NULL test
date is deliberately *not* applied in the grouping path — see the grouping decision above.

**`class_profiles` follows the ADR 0007 storage precedent: file on disk, row in the DB.** The row
carries the grouping key, the derived norm group, the child count, the artifact path, and — the
reason the table exists in this shape — `assessment_ids_json`, the exact set of validated
assessments the report was built from. That set is what "stale" means: a profile is stale when it
differs from the group's current validated id set. Profiles are append-only; regenerating inserts a
new row and leaves the old artifact on disk, because a report handed to a teacher must stay
retrievable exactly as issued. No foreign key to `assessments`: the id list is a historical record,
and deleting an assessment must not mutate an issued report. The table is additive (`CREATE TABLE IF
NOT EXISTS`), so no migration and no data loss — unlike ADR 0007, nothing is discarded.

**The artifact is markdown with inline SVG. PDF export is deferred to a later iteration.** One
source string is rendered on screen via `st.markdown(unsafe_allow_html=True)` and written to
`data/reports/`. No PDF library is added now: `weasyprint` would require a non-Python
`brew install pango` step on a machine that currently needs none, and headless-browser and
pure-Python alternatives each carry their own cost, so none is worth committing to before the
document's shape has been seen in practice. This is a deferral, not a gap in the format: markdown +
inline SVG is precisely the input every evaluated renderer consumes, so adding one later is a new
module behind a one-function adapter plus a `pdf_path` column that already exists — no rework of the
artifact. Charts stay vector and theme-consistent (reusing the existing `--demat-*` palette as
literal hex so colour survives outside the app's CSS) and are restricted to a conservative SVG
subset for the same forward-compatibility reason.

**`SkillRunner` grows `run_text()` rather than having `run()` widened.** `run()` requires a
positional `image_path` and `DirectMessagesRunner` hardcodes its output model to `TriageResult` or
`ExtractedSheet`, so a text-in/schema-out prose call cannot go through it as written. A sibling
`run_text(prompt, output_path, output_model)` on the Protocol and both runners leaves `run()` and its
single call site (`demat/extraction.py:51`; no fake runners exist in `tests/`) untouched, and keeps
`agent_runner` the only module importing `claude_agent_sdk` or `anthropic`.

## Alternatives rejected

- **Replacing the Assessment Review page instead of Results.** An earlier reading of the request.
  Rejected: `store.finalize_assessment()` is reachable only from that page, so removing it would
  leave the app with no path to a terminal state — and since only validated assessments are eligible
  for a profile, no profile could ever be generated. The feature would have removed its own input.
- **Keeping Results as a fourth nav page.** Would preserve the cross-class CSV export at the cost of
  a nav slot for a table the profile largely supersedes. A per-class export on the profile page
  covers the realistic use.
- **Letting the model read the sheets, or the norm tables, and write the numbers.** The fastest path
  to a report and the only one that can silently put a wrong Prozentrang in front of a teacher. The
  numbers are already extracted and already human-validated; regenerating them would discard that
  work and violate KB rule 2.
- **Trusting the prompt to keep the model off numbers and diagnoses.** Prompts describe intent;
  validators enforce it. The two checks cost a few dozen lines and turn both KB rules into failures
  rather than into text a reviewer has to catch.
- **A blanket ban on the diagnosis terms.** Would forbid „ggf. Förderdiagnostik erwägen", which is a
  legitimate and useful recommendation, and would be disabled by the first person who needed it.
- **Handing `scripts/lookup.py` to the agent as a Bash tool.** Would require relaxing the runner's
  `disallowed_tools`, trading a hard architectural guarantee for convenience, and would make the
  norm values a model output again.
- **Reproducing the sample document's four PR buckets for every version.** They are a simplification
  of DEMAT 2+'s six printed categories, and DEMAT 1+ and 5+ do not classify by PR at all. Using them
  everywhere would misreport two of the four versions.
- **A user-selected comparison norm, or a `default_norm_group` field in the test-definition TOML.**
  Both were considered and both are wrong: a class takes one sheet at one time, so the norm group is
  a function of data the grouping key already pins down. A selector would offer a choice that has
  exactly one correct answer, and a TOML field would make a per-report fact look like a per-version
  constant — DEMAT 2+ legitimately uses `k2_gesamt` or `k3_gesamt` depending on when the class sat
  the test. Deriving it also avoids extending ADR 0005's definition schema.
- **Using the gender-split norm groups.** A class report describes one mixed group; `_gesamt` is the
  matching series. Splitting a class report by gender is a different feature.
- **Hardcoding "DEMAT 3+ has class norms" in report code.** Duplicates knowledge that already lives
  in the test definition's `norm_test`, and would need editing to add a version.
- **Including `test_form` in the class key.** Would split one taught class into two reports for no
  psychometric reason.
- **Splitting a multi-date group into one report per Testdatum.** A class tested over two sittings is
  one class; splitting fragments the cohort the teacher works with.
- **Applying `school_year()`'s today-fallback when grouping.** A validated sheet with no test date
  would join whatever Schuljahr happens to be current at report time, would silently change groups
  across the August boundary, and would get a norm group derived from a date that does not exist.
  Ungrouped-and-counted is the honest handling.
- **Storing `schuljahr` as a column on `assessments` so classes can be grouped in SQL.** Duplicates a
  derived value that would drift the moment `test_date_iso` is corrected in review. Grouping in
  Python over a small local table is cheap.
- **Detecting staleness from `generated_at` vs `finalized_at`, or from the child count.** A timestamp
  comparison misses an edit to an already-included row; a count matches after an add plus a removal.
  The validated id set is the only rule that is right in both cases.
- **Overwriting a profile on regeneration.** Cheaper, but a report shown to a teacher or filed must
  stay retrievable exactly as issued.
- **Adding a PDF renderer in this iteration.** Every candidate carries a cost worth paying only once
  the document is settled: weasyprint needs system cairo/pango, playwright needs a ~150 MB browser,
  and the pure-Python options support inline SVG so poorly they would force raster charts. Deferring
  costs nothing because the artifact format is already the renderers' input format.
- **matplotlib/PNG charts.** Raster, off-theme, a new heavyweight dependency, and they would not
  match the app's palette in both the on-screen and any future PDF rendering.
- **A separate module constructing its own Anthropic client** for the prose call. Breaks
  `agent_runner`'s documented invariant as the only module importing the SDKs, for one feature's
  convenience.
- **Widening `SkillRunner.run()` with an optional `image_path` and an `output_model`.** Workable, but
  it loads the extraction path's signature with report-only parameters; a sibling method costs less.
- **Naming the feature „Klassenprofil" in code, paths and UI.** The generated document is German;
  the codebase and its chrome are English. Mixing the two would put a German noun in table names,
  module paths and nav labels while every neighbouring identifier is English.
