# ADR 0006 — Person identity spans tests and school years: klasse leaves the ID key

- Status: Accepted
- Date: 2026-09-20
- Amends: ADR 0001

## Context

A new requirement: *"tested children will likely go through multiple tests, so make sure to
retain that history, per child."*

**Structurally, the store already supports this.** `assessments.person_id` is a plain,
non-unique foreign key, so N assessment rows accumulate per person and
`WHERE person_id = ? ORDER BY test_date_iso` is the history query. `subtest_scores` and
(per ADR 0005) `assessment_metrics` hang off `assessment_id`, not off the person, so nothing
is overwritten by a second test. No schema change is needed for history to *exist*.

**But the ID derivation defeats it.** ADR 0001 keys the ID on
`"<normalized name>|<normalized klasse>"` and lists, under rejected alternatives:

> **Name only, no class, in the key.** … Accepted tradeoff: a child re-tested in a new class
> gets a new ID.

That tradeoff was priced against a requirement that did not include cross-test history. The
dominant real case for "multiple tests per child" is *a year apart* — DEMAT 2+ in class 2b,
DEMAT 3+ in class 3b. Under the current key those are two different `person_id`s, two
`persons` rows, and no history at all. The mechanism intended to prevent corruption is
silently producing the exact outcome the new requirement forbids — and producing it
*invisibly*: nothing flags a fractured person, they simply look like two children.

Also relevant: `persons.klasse` and `persons.gender` are populated from whichever assessment
created the row and never updated, so they are already "class the child was in the first time
we saw them" masquerading as current facts.

## Decision

**1. The normalization key is the normalized name alone.**

```
normalization_key = normalize_name(child_name)      # klasse no longer a component
```

`normalize_name` (NFKC → German folding → lowercase → non-alphanumeric to space → collapse)
is unchanged; ADR 0001's algorithm, salt handling and hash-collision policy are all unchanged.
Only the key composition changes. `identity.normalization_key(name, klasse)` loses its
`klasse` parameter.

**2. `klasse` stays, per assessment, where it was always accurate.** `assessments.klasse` is
already written from each sheet. `persons.klasse` and `persons.gender` are **dropped** —
a person's class is a fact about a point in time, not about the person, and keeping a stale
copy on `persons` invites reads that are wrong by one school year. Current class is
`klasse` from that person's most recent assessment, which is a query, not a column.

**3. The re-introduced same-name risk gets one detector, not a framework.** Name-only keying
means two genuinely different children written "Leon Müller" merge into one person, silently
and permanently. That is a real regression and the reason ADR 0001 chose otherwise. The
mitigation: when a new assessment links to an **existing** person and both the stored gender
(from that person's prior assessments) and the incoming gender are definite — neither is
`unknown` — and they differ, raise `possible_person_merge`, set `needs_review`, and record
both values in `review_notes`. The assessment is still stored and still linked; the flag is
an invitation to look, not a block.

Deliberately **only** that rule. A klasse-based heuristic was considered and rejected: 2b→3b
is normal progression, 3a→3b is ambiguous between a class change, a repeated year and a
different child, and 3a→4a a year later is exactly what we now want to *encourage*. A flag
that fires on normal progression is a flag the user learns to dismiss, which would also cost
us the gender rule's credibility.

`person_names` (already present, already storing every distinct raw spelling per person,
already suppressible via `STORE_RAW_NAMES`) is the human's resolution tool when a flag fires.

**4. `source_page_key` UNIQUE is correct as-is and does not block retests — but the stated
mechanism matters.** It is worth being precise, because the intuitive justification is wrong.
The column is not a hash of page pixels. `pdf_render._page_key` computes
`sha256(f"{file_sha256}:{page_index}")` — the identity of the *source page*, deliberately not
of the rendered PNG (ADR 0003: hashing the PNG would make dedup depend on DPI). So:

- *Same child, retakes the same test version later* → new physical sheet → new scan → new PDF
  bytes → different `file_sha256` → different key. Never blocked. ✅
- *Same child, same test, scanned twice* → two different PDFs → two different keys → two rows.
  Not blocked either — which is the right default (we cannot tell a rescan from a retake) but
  means the constraint is weaker than "content dedup": re-exporting the same page into a new
  PDF also produces a duplicate row.
- *Literally the same PDF uploaded twice* → identical bytes → identical key → blocked. ✅
  This is the case the constraint exists for, and the only one it catches.

The constraint therefore **stays**, and is **renamed `source_page_key`** — the current name
`source_page_sha256` reads as a content hash and is what produced the wrong intuition.

For the undetected-duplicate direction, a soft check: on save, if an assessment already exists
with the same `person_id`, `test_version` and non-null `test_date_iso`, flag
`duplicate_suspected` and set `needs_review`. This does **not** contradict ADR 0003's
rejection of `(person, test, date)` as a natural key — that was rejected *as a key* precisely
because the date may be misread, and a misread date must never silently drop a record. As a
soft flag the failure mode inverts: a misread date merely fails to fire the flag.

## Consequences

- Existing `person_id` values change. There is no production data (see ADR 0005 §7); the
  `data/` reset covers this. Salt handling is untouched, so IDs remain install-scoped.
- Same-name children in different classes now merge by default instead of separating by
  default. This is a deliberate reversal of ADR 0001's priority: an over-merge is visible in
  the UI (two test dates that cannot both belong to one child, mismatched genders flagged) and
  repairable, whereas the fracture it replaces was invisible.
- If same-name merges turn out to bite in practice, the escape hatch is a manual
  `persons`-split / re-link action in the Results tab plus a `person_overrides` mapping —
  additive, and deliberately deferred until there is evidence it is needed.

## Alternatives rejected

- **Keep klasse in the key; link history via a new `person_links` merge table + merge UI.**
  Strictly more correct — it preserves ADR 0001's separation *and* gives cross-year history.
  But it needs a link table, a UI, and every history query rewritten to traverse links, and it
  defaults to "no history until a human merges", which is the failure the requirement is
  about. Too much machinery for a single-user local app; revisit if collisions become real.
- **Key on name + gender.** Halves nothing useful — same-name collisions in a German school
  cohort are overwhelmingly same-gender ("two Leons") — while adding a fracture risk on the
  one field the model regularly reads as `unknown` from a smudged checkbox. Worse on both axes
  than name-only-plus-flag, which uses gender as a *detector* rather than a *key component*.
- **Key on name + birth year / date of birth.** The genuinely correct disambiguator, but it is
  not printed on the DEMAT score sheet, so there is nothing to extract.
- **Key on name + school (multi-school install).** Irrelevant today — one install, one school.
  If it stops being true, this is where it would go.
- **Auto-merge fractured persons by name after the fact.** Same end state as name-only keying,
  reached through a background job with the extra failure mode of merging records a human had
  already reviewed as distinct.
