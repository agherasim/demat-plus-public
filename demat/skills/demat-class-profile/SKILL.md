---
name: demat-class-profile
description: Draft the interpretive prose sections of a DEMAT Classroom Profile report from already-computed class data — never computes or invents a number.
---

# DEMAT Classroom Profile — prose

You are given the complete computed data for one class's DEMAT Classroom Profile as a JSON
attachment (a `ClassProfileData` object: every child's already-validated scores, the class
distribution, per-subtest means, and each child's deterministic Stärken/Schwächen candidates).
Your only job is to write the interpretive prose. Write nothing else.

## Output contract

Write exactly one JSON object matching the `ClassProse` schema to the output path given in the
prompt:

```json
{
  "profil_staerken": "...",
  "profil_schwaechen": "...",
  "profil_muster": "...",
  "empfehlungen": ["...", "..."],
  "kinder": [
    {"person_ref": "<person_id from the input>", "staerken": "...", "schwaechen": "...", "empfehlungen": ["..."]}
  ]
}
```

`kinder` must contain exactly one entry per child in the input, using that child's `person_ref`
verbatim as the join key — never a name, never omit or invent one.

Never write a straight `"` inside a string value, escaped or not — it is never needed for German
prose. If you catch yourself about to type one, you are about to quote a manual term; use „…“
instead (see rule 1).

## Hard rules

1. **German output.** Keep the manual's terms as-is: Prozentrang, T-Wert, „unterdurchschnittlich“,
   Teilbereich, Förderung. Do not transliterate special characters. **Quote a manual term with
   „…“ (U+201E … U+201C) — never a straight `"` as the closing mark.** A straight `"` is your
   output file's own JSON string delimiter; writing one unescaped inside a string breaks the
   file's JSON syntax and fails the whole run. „…“ is the correct German pairing anyway, and,
   unlike a straight quote, never needs escaping.
2. **Every number you write must appear verbatim in the input JSON.** Do not compute, average,
   round, rank, or infer any number. Do not convert between PR and T-Wert. Write quantities as
   digits ("12"), never as German number words ("zwölf") — a validator checks this mechanically
   and a spelled-out number would defeat it, so digits are required, not just preferred.
3. **Never state or imply a diagnosis** (KB rule 6). A Rechenstörung / Rechenschwäche /
   Dyskalkulie cannot be concluded from a DEMAT score by itself. You may recommend *further assessment*
   ("Förderdiagnostik erwägen", "weitere Abklärung", "ggf. abklären") — that is a referral, not a
   diagnosis, and is a legitimate recommendation. Never write a bare assertive form ("X hat eine
   Rechenschwäche", "bei X liegt eine Dyskalkulie vor").
4. **Base judgements on the total score; read subtest results as indications**
   (KB rule 6) — do not over-interpret a single weak or strong subtest.
5. **Refer to children by `person_ref` only.** Never use a name — you were not given one. Name
   substitution for display happens after your output, in a later stage.
6. **Recommendations are classroom actions**, not clinical ones (differentiation, practice
   material, small-group work, revisiting a specific Teilbereich) — not therapy or medical
   referral language beyond the referral carve-out in rule 3.

## Content guidance

- `profil_staerken` / `profil_schwaechen` / `profil_muster`: one short paragraph each, describing
  what the class distribution and per-subtest means show — strengths, clear weaknesses, and any
  notable pattern (e.g. one Teilbereich pulling the mean down, a bimodal split, high variance).
  Base these only on the `distribution` and `subtest_rows` in the input.
- `empfehlungen`: 4-8 short bullets, concrete classroom actions for the whole class.
- Per child (`kinder`): `staerken` — one short sentence, from that child's `strengths` subtest
  keys in the input. `schwaechen` — one short sentence from `weaknesses`, or `""` when the input
  lists none. `empfehlungen` — 1-4 short bullets, concrete and specific to that child's pattern.

Your output is validated mechanically before anything is saved: a number not present in the input
JSON, or a bare diagnosis assertion, fails the run and you will be asked to correct it.
