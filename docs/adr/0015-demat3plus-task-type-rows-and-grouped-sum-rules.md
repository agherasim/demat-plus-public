# ADR 0015 — DEMAT 3+ is modelled as it is scored: Aufgabentyp rows, grouped RW sums, and per-subtest PR

- Status: Proposed
- Date: 2026-09-27
- Extends: ADR 0005 (test definitions, `[[rules.sum]]`), ADR 0013 (norm completion, per-cell provenance)
- Amends: ADR 0013's RW(ges) sum-fill rule and its "Out of scope" paragraph (subtest PR-Band / T),
  and ADR 0005's DEMAT 3+ scope note ("task types out of scope")

## Context

`demat3plus.toml` was written from the knowledge base (KB) before anyone had seen a DEMAT 3+
sheet. It declares three subtests (ARI, SAR, GEO) and a Gesamtwert block (`rw_total`,
`pr_total`, `pr_band`, `t_total`, `t_band`). It treats the sheet like DEMAT 2+: the teacher
writes a Rohwert (RW) per subtest, and the app looks up the norm values.

We now have real DEMAT 3+ sheets: Form B, class 4b, 23.09.2026, `anfang_k4`. They are stored
as page images under `data/pages/`, and their extractions are in `data/demat.db`. Two page
images were read directly (`c291ae50…png`, which is assessment 8, and `97aa642e…png`). Both
show the same printed layout:

1. **An Aufgabentyp grid with 9 rows** (ZS, AD, SU, MU, SR, SZ, FL, LS, LU). Each row's RW
   cell sits in the column of the subtest it belongs to: Arithmetik, Sachrechnen or Geometrie.
   SR has **two** cells, `SR1 (2)` and `SR2 (2)`, so the grid has 10 RW cells. A right-hand
   column holds fields `A`–`I`, one per Aufgabentyp; SR's field `E` has maximum 4. That column
   is empty on both sheets.
2. **„Rohwert-Summe“**, fields `J (15)`, `K (8)`, `L (8)` and `M (31)`, under the headings
   „Subtest Arithmetik / Sachrechnen / Geometrie / Test-gesamtwert“.
3. **„Prozentrang:“**, one empty numeric cell per column J–M.
4. **„Prozentrang-Band“**, a 0–100 graph with one column per J–M.
5. **No T-Wert cells, and no Junge/Mädchen box.**

The teacher fills in the **Aufgabentyp** RWs. On both sheets J/K/L/M are either empty or only
partly filled (assessment 8: J=3, K=0, L=2, M empty). The subtest RWs therefore arrive as
data the app has to derive, and the current model has no field for their inputs.

The manual confirms that this is the intended procedure (KB `references/demat3plus.md`,
Kap. 7.1): „Summen je Aufgabentyp → Subtests → Testgesamtwert (0–31)“. The Aufgabentyp →
subtest grouping is KB-verified (manual S. 10, Tabelle 2):

- ARI = ZS(3) + AD(4) + SU(4) + MU(4) = 15
- SAR = SR(4) + LU(4) = 8
- GEO = SZ(3) + FL(3) + LS(2) = 8
- 15 + 8 + 8 = 31

The sheet prints SR as SR1(2) + SR2(2). This matches the manual's two SR test parts (S. 10–11
and S. 12–13), but the KB does not state the 2 + 2 split itself.

**The Gesamtwert is normed.** The KB publishes tables A4 (`ende_k3`) and A8 (`anfang_k4`) on
scale `gesamtwert`, with PR, PR-Band, T and T-Band. The manual's own case examples (S. 26)
report the Gesamtwert together with its PR. The sheet prints a Prozentrang
cell and a PR-Band column for „Test-gesamtwert“ (M), and the manual says to draw the band „je
Subtest und für den Gesamtwert“. So a `pr_total` exists. The sheet has no T cells at all.

The current extractions are **inconsistent across identical sheets**, and the cause is the
extraction contract, not the layout. The stub skill and the injected prompt contract list
only ARI/SAR/GEO and call the task types out of scope. The model therefore made a different
choice on each sheet:

- It emitted the 10 task rows plus null ARI/SAR/GEO.
- It dropped the task rows and emitted null ARI/SAR/GEO.
- On assessment 8, it emitted only the J/K/L values the teacher had written. The image
  confirms that these are handwritten and agree with the task rows (2+0+0+1=3, 0+0+0=0,
  0+2+0=2).

## Decision

**1. Aufgabentyp cells are subtest rows of the definition, not a new table.**
`demat3plus.toml` declares the 10 printed RW cells as `[[subtests]]`, with no `norm_scale`:

- ZS, AD, SU, MU, SR1, SR2, SZ, FL, LS, LU, in printed order;
- then ARI, SAR, GEO, with their existing `norm_scale`s.

The task rows reuse the whole existing subtest pipeline: canonical keys, `subtest_scores`,
the review editor, `rw_exceeds_max`, provenance and the CSV. A row with no `norm_scale`
already becomes `unavailable` / `no_norm_scale` under ADR 0013, so task rows never raise a
norm warning.

**2. `[[rules.sum]]` gains a grouped form.** A rule names **one** target, either
`subtest = "<key>"` or `metric = "<key>"`, and an explicit `over = [<subtest keys>]`:

```toml
[[rules.sum]]
subtest = "ARI"
over    = ["ZS", "AD", "SU", "MU"]
[[rules.sum]]
subtest = "SAR"
over    = ["SR1", "SR2", "LU"]
[[rules.sum]]
subtest = "GEO"
over    = ["SZ", "FL", "LS"]
[[rules.sum]]
metric  = "rw_total"
over    = ["ARI", "SAR", "GEO"]
```

- The legacy `over = "subtests.rw"` (metric target only) keeps its current meaning for DEMAT
  1+/2+/5+, byte for byte.
- A definition that has any subtest-target rule must not use the legacy form. Otherwise
  summing "all subtests" would count every point twice (62 instead of 31).
- Load-time validation:
  - Every key exists.
  - Subtest-target rules do not chain: their `over` contains no other target.
  - Each component belongs to at most one subtest-target rule.
  - Where every `rw_max` is set, the components' maxima sum to the target's `rw_max`, and a
    metric target's components sum to its `max_possible`.

  This is the anti-drift tripwire for the grouping itself.
- Which rows are "Aufgabentyp" rows is **derived** from the rules (the union of subtest-target
  `over` lists). It is not declared a second time: one source of truth, and no catalog column.

**3. Every consumer evaluates a rule over its own `over` set.** This covers the three places
that sum today:

- `validation.validate` (`total_mismatch`);
- `review_validation._check_values_consistent`;
- `norm_completion.plan_completion`.

For list-form rules each of them sums only the listed keys. Summing every row, which is
today's behaviour, would put a blocking error on every 3+ sheet.

**4. Subtest RWs may be sum-filled, with per-cell provenance, the same way RW(ges) already is.**

- `SubtestScore` gains `rw_state` (`written | blank | illegible | None`).
- `subtest_scores` gains an additive `rw_provenance TEXT` column, with ADR 0013's vocabulary
  and its no-backfill rule.
- `save_assessment` sets `rw_provenance = 'blank'` only when both hold:
  - the RW is null and reported `blank`;
  - the key is a subtest-sum target in the definition.

  Everywhere else it sets `'sheet'`, so the field is inert for 1+/2+/5+.
- Completion fills a `blank` target when every component is present exactly once with a
  numeric RW. It never overwrites a written, illegible or manual RW. An RW edit by the
  reviewer makes the cell `manual`.
- The planner builds one **effective RW map**: stored RWs, then subtest-target sums, then
  metric-target sums. Every later step reads from it, so the subtest PR lookup and the
  Gesamtwert lookup see an RW computed in the same run. There is no second pass.

This is still arithmetic on the teacher's own numbers, not a norm lookup. ADR 0013's rule
that "RW(ges) may be filled by arithmetic" now covers any declared sum.

**5. The DEMAT 3+ Gesamtwert block keeps `rw_total`, `pr_total`, `pr_band`, and drops `t_total`
and `t_band`.**

- `pr_total` and `pr_band` are KB-backed (A4/A8) and have a place on the sheet.
- `pr_total` is also the classification input: DEMAT 3+ classifies by PR (Tabelle 4), and
  `build_class_data` raises if no overall metric carries `norm_field = "pr"`.
- `t_total` and `t_band` have no cell on the sheet. The model already omits them, and
  completion never inserts rows. Declaring them would only invite a hallucinated cell.
- The KB's T values remain available if a later feature needs them.

**6. Per-subtest PR stays the existing `subtest_scores.pr`.** It is now reachable, because
ARI/SAR/GEO get an RW. Subtest PR-Band, T and T-Band remain out of scope. The trigger ADR
0013 set for revisiting ("a real 3+ sheet shows those cells") has now fired, and was checked:

- T: the sheet shows no subtest T cells.
- PR-Band: the sheet shows the band only as a **hand-drawn graph**, not as a transcribable
  value. That does not fit the observe-then-fill cell contract, and no consumer reads subtest
  bands yet.

Adding them later means an additive `subtest_scores.pr_band` + `pr_band_provenance` pair and
a definition flag. Nothing here blocks that.

**7. The DEMAT 3+ extraction skill stops being a stub.**

- It transcribes all 10 Aufgabentyp RW cells, always.
- It transcribes J/K/L into ARI/SAR/GEO and M into `rw_total`, each with its state.
- It **never** adds cells up itself.
- The right-hand A–I column is not transcribed as rows. If it is filled and disagrees with the
  grid, the skill notes it in `illegible_notes`.

`prompt_contract()` renders each subtest-sum group, so the prompt and the skill say the same
thing.

**8. Classification is unchanged in kind.** DEMAT 3+ classifies the overall result by PR from
`pr_total` (scale `gesamtwert`, table A4/A8, Tabelle 4). Subtest strengths and weaknesses use
the ARI/SAR/GEO PRs. Task rows carry no PR and are excluded from report subtest aggregates.

## Alternatives rejected

- **"There is no `pr_total`; only the subtests have a PR."** This contradicts the KB (A4/A8
  Gesamtwert norms, and the Fallbeispiele PR 92/17), the sheet (Prozentrang and PR-Band
  columns for „Test-gesamtwert“), and the code: dropping the only `norm_field="pr"` metric
  breaks the Class Profile.
- **A separate `task_type_scores` table.** It would duplicate canonical-key resolution, the
  editor, provenance, validation and the CSV for rows that behave exactly like subtests
  without norms.
- **Storing Aufgabentypen as overall metrics.** It loses the grouping and reuses the
  Gesamtwert contract for something that is not a Gesamtwert.
- **Declaring a `level`/`parent` field on each subtest in addition to the rules.** That is two
  sources of truth for one grouping, plus a catalog column. It is derived from the rules
  instead.
- **Making `over = "subtests.rw"` mean "top-level subtests only".** This silently changes the
  meaning of a rule stored in every other definition's `spec_json`. An explicit list cannot
  be misread.
- **Chained / nested sums (SR = SR1 + SR2, then SAR = SR + LU).** No current consumer needs an
  SR total. The class task-type norm for SR, if built later, can sum SR1 + SR2 itself. One
  level of subtest sums keeps ordering trivial.
- **Letting the model sum the task rows into J/K/L/M.** This breaks "never compute a total that
  is not printed" (ADR 0003/0005). It is also exactly what makes a model-summed value
  indistinguishable from a handwritten one.
- **Filling only when the task rows and J/K/L all agree.** The normal sheet has J/K/L empty.
  When both are present, the existing mismatch check already reports disagreement.
- **Keeping `t_total`/`t_band` "because the KB has them".** The definition is the extraction
  contract for the sheet. A metric with no cell is either never filled (completion does not
  insert rows) or invented by the model.
- **Adding subtest PR-Band now.** See Decision 6: the only source on the sheet is a drawn
  graph, and nothing consumes it yet.
- **Backfilling `rw_provenance` or rewriting the existing 3+ rows.** This follows ADR 0010/0013.
  The six affected rows are re-extracted instead.
