"""Stage 2 — data assembly (ADR 0008 spec §6.1). Pure functions over
`store` results; no Streamlit, no LLM, no network, no KB subprocess calls
(those are stage 3, `demat/reports/norms.py`; classification is injected
here via `classify_fn`). Every number in the report is computed here.
"""

from __future__ import annotations

import statistics
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, Field

from demat.test_registry import TestDefinition
from demat.validation import coerce_float

ClassifyFn = Callable[[float | None], dict[str, Any]]


class ChildData(BaseModel):
    assessment_id: int
    person_id: str | None
    person_ref: str  # join key back to the data; person_id, or a stable
    # synthetic ref for an unlinked child (never a name — spec §6.4 rule 5)
    display_label: str
    overall_value: float | None
    overall_category: str | None
    overall_categories: list[str] | None = None  # boundary double-category
    classification_note: str | None = None
    subtest_pr: dict[str, int | None]
    subtest_rw: dict[str, float | None]
    subtest_category: dict[str, str | None] = Field(default_factory=dict)
    strengths: list[str] = Field(default_factory=list)  # subtest keys, best first
    weaknesses: list[str] = Field(default_factory=list)  # subtest keys, worst first


class BucketMember(BaseModel):
    person_ref: str
    display_label: str
    value: float | None


class DistributionBucket(BaseModel):
    category: str
    ordinal_rank: int
    count: int
    members: list[BucketMember]


class SubtestRow(BaseModel):
    key: str
    name: str
    printed_label: str | None
    mean_pr: float | None
    n_present: int
    n_children: int
    category: str | None


class MissingDataAudit(BaseModel):
    children_without_overall: list[str]
    subtests_with_zero_present: list[str]


class ClassProfileData(BaseModel):
    klasse: str
    schuljahr: str
    test_version: str
    test_key: str
    norm_test: str
    test_form_summary: str | None
    test_date_min: str | None
    test_date_max: str | None
    child_count: int
    awaiting_review_count: int
    classification_basis: str  # "pr" | "t"
    categories_ordered: list[str]  # worst -> best
    norm_group: str | None
    norm_group_label: str | None
    norm_variant: str | None
    cross_check_warning: str | None
    children: list[ChildData]
    distribution: list[DistributionBucket]
    class_mean: int | None  # rounded to integer for display (spec §6.1)
    class_median: int | None
    class_n: int
    subtest_rows: list[SubtestRow]
    missing_data: MissingDataAudit
    has_class_norms: bool  # norm_test == "demat3plus"
    class_norm: dict[str, Any] | None = None  # stage 3's `norms.class_norm()` output


def _person_ref(person_id: str | None, assessment_id: int) -> str:
    return person_id if person_id else f"unlinked-{assessment_id}"


def _display_label(
    person_id: str | None,
    assessment_id: int,
    raw_name: str | None,
    store_raw_names: bool,
) -> str:
    if store_raw_names and raw_name:
        return raw_name
    if person_id:
        return person_id
    return f"unlinked #{assessment_id}"


def _child_strengths_weaknesses(
    subtest_pr: dict[str, int | None],
    classification_basis: str,
    categories_ordered: list[str],
    classify_fn: ClassifyFn,
    *,
    top_n: int = 2,
) -> tuple[list[str], list[str]]:
    """Deterministic Stärken/Schwächen candidates (spec §6.1): subtests in
    the version's top/bottom manual categories, sorted by value. Handed to
    stage 4 as facts — the model phrases them, never selects them.

    For a PR-classified version (2+/3+) this reuses `classify_fn` (already
    bound to `--pr`, the same basis as the overall score) on each present
    subtest PR — the manual's own bands. For a T-classified version (1+/5+)
    the manual publishes no subtest-level classification (subtests never
    carry a T-Wert on the sheet), so this falls back to ranking the child's
    own present subtest PRs directly — an approximation, not a manual
    category; documented as such in the implementation notes.
    """
    present = [(key, pr) for key, pr in subtest_pr.items() if pr is not None]
    if not present:
        return [], []

    if classification_basis == "pr":
        top_categories = set(categories_ordered[-top_n:])
        bottom_categories = set(categories_ordered[:top_n])
        scored = [(key, pr, classify_fn(pr).get("category")) for key, pr in present]
        strong_scored = sorted(
            (s for s in scored if s[2] in top_categories),
            key=lambda s: s[1],
            reverse=True,
        )
        weak_scored = sorted(
            (s for s in scored if s[2] in bottom_categories),
            key=lambda s: s[1],
        )
        return [s[0] for s in strong_scored], [w[0] for w in weak_scored]

    ranked_desc = sorted(present, key=lambda kv: kv[1], reverse=True)
    ranked_asc = sorted(present, key=lambda kv: kv[1])
    top_keys = [k for k, _ in ranked_desc[:top_n]]
    bottom_keys = [k for k, _ in ranked_asc[:top_n] if k not in top_keys]
    return top_keys, bottom_keys


def overall_metric_key(definition: TestDefinition, classification_basis: str) -> str:
    """The `overall_metrics` key whose `norm_field` matches the version's
    classification basis ('pr' -> `norm_field == "pr"`, 't' ->
    `norm_field == "t_wert"`). Raises `ValueError` if the definition
    declares none — every active definition must (spec §6.2.2).
    """
    for m in definition.overall_metrics:
        if classification_basis == "pr" and m.norm_field == "pr":
            return m.key
        if classification_basis == "t" and m.norm_field == "t_wert":
            return m.key
    raise ValueError(
        f"{definition.test_version}: no overall metric with "
        f"norm_field matching basis {classification_basis!r}"
    )


def build_child_data(
    *,
    assessment_id: int,
    person_id: str | None,
    raw_name: str | None,
    store_raw_names: bool,
    overall_key: str,
    metrics: list[dict[str, Any]],
    subtests: list[dict[str, Any]],
    classification_basis: str,
    categories_ordered: list[str],
    classify_fn: ClassifyFn,
    component_keys: set[str],
) -> ChildData:
    """One child's row of `build_class_data()`, extracted unchanged (spec
    §11) so the child-report pipeline can build the same `ChildData` for a
    single assessment without re-deriving the class distribution.

    `component_keys` are the Aufgabentyp rows (DEMAT 3+, ADR 0015) that
    only exist to be summed into a subtest and carry no PR — keep them out
    of every subtest aggregate. Empty for 1+/2+/5+; pass
    `definition.component_keys()`.
    """
    person_ref = _person_ref(person_id, assessment_id)
    display_label = _display_label(person_id, assessment_id, raw_name, store_raw_names)

    overall_value: float | None = None
    for mv in metrics:
        if mv["metric_key"] == overall_key:
            overall_value = (
                mv["value_num"]
                if mv["value_num"] is not None
                else coerce_float(mv["value_raw"])
            )
            break

    overall_category = None
    overall_categories = None
    classification_note = None
    if overall_value is not None:
        result = classify_fn(overall_value)
        overall_category = result.get("category")
        overall_categories = result.get("categories")
        classification_note = result.get("note")

    subtest_pr: dict[str, int | None] = {}
    subtest_rw: dict[str, float | None] = {}
    for st in subtests:
        key = st["subtest_key"]
        if key in component_keys:
            continue
        subtest_pr[key] = st.get("pr")
        subtest_rw[key] = st.get("rw")

    strengths, weaknesses = _child_strengths_weaknesses(
        subtest_pr, classification_basis, categories_ordered, classify_fn
    )
    subtest_category: dict[str, str | None] = {}
    if classification_basis == "pr":
        for key, pr in subtest_pr.items():
            subtest_category[key] = (
                classify_fn(pr).get("category") if pr is not None else None
            )

    return ChildData(
        assessment_id=assessment_id,
        person_id=person_id,
        person_ref=person_ref,
        display_label=display_label,
        overall_value=overall_value,
        overall_category=overall_category,
        overall_categories=overall_categories,
        classification_note=classification_note,
        subtest_pr=subtest_pr,
        subtest_rw=subtest_rw,
        subtest_category=subtest_category,
        strengths=strengths,
        weaknesses=weaknesses,
    )


def build_class_data(
    *,
    klasse: str,
    schuljahr: str,
    test_version: str,
    test_form_summary: str | None,
    test_date_min: str | None,
    test_date_max: str | None,
    awaiting_review_count: int,
    assessment_rows: list[dict[str, Any]],
    metrics_by_assessment: dict[int, list[dict[str, Any]]],
    subtests_by_assessment: dict[int, list[dict[str, Any]]],
    raw_names_by_assessment: dict[int, str | None],
    store_raw_names: bool,
    definition: TestDefinition,
    classification_basis: str,
    classify_fn: ClassifyFn,
    norm_group: str | None,
    norm_group_label: str | None,
    norm_variant: str | None,
    cross_check_warning: str | None,
    class_norm: dict[str, Any] | None = None,
) -> ClassProfileData:
    """Assemble every deterministic number in the report from already-
    validated, already-extracted data. `classify_fn` wraps stage 3's
    `norms.classify()` for one basis-appropriate value at a time — the
    band boundaries themselves still come from the KB, never from this
    module.
    """
    from demat.reports.norms import CATEGORIES_ORDERED

    categories_ordered = CATEGORIES_ORDERED[definition.norm_test or ""]
    overall_key = overall_metric_key(definition, classification_basis)

    # Aufgabentyp rows (DEMAT 3+, ADR 0015) only exist to be summed into a
    # subtest and carry no PR — keep them out of every subtest aggregate.
    # Empty for 1+/2+/5+.
    component_keys = definition.component_keys()

    children: list[ChildData] = [
        build_child_data(
            assessment_id=row["assessment_id"],
            person_id=row.get("person_id"),
            raw_name=raw_names_by_assessment.get(row["assessment_id"]),
            store_raw_names=store_raw_names,
            overall_key=overall_key,
            metrics=metrics_by_assessment.get(row["assessment_id"], []),
            subtests=subtests_by_assessment.get(row["assessment_id"], []),
            classification_basis=classification_basis,
            categories_ordered=categories_ordered,
            classify_fn=classify_fn,
            component_keys=component_keys,
        )
        for row in assessment_rows
    ]

    # --- Distribution buckets ------------------------------------------
    buckets_by_category: dict[str, list[BucketMember]] = {
        c: [] for c in categories_ordered
    }
    for child in children:
        if child.overall_value is None:
            continue
        # A boundary double-category (spec §6.2.2) counts the child once,
        # in the LOWER (worse) category, and carries the note; render both
        # category names via `classification_note`/`overall_categories`.
        target = child.overall_category
        if target is None and child.overall_categories:
            ranked = [c for c in categories_ordered if c in child.overall_categories]
            target = ranked[0] if ranked else None
        if target is None or target not in buckets_by_category:
            continue
        buckets_by_category[target].append(
            BucketMember(
                person_ref=child.person_ref,
                display_label=child.display_label,
                value=child.overall_value,
            )
        )

    distribution = [
        DistributionBucket(
            category=cat,
            ordinal_rank=i,
            count=len(members),
            members=members,
        )
        for i, (cat, members) in enumerate(
            (c, buckets_by_category[c]) for c in categories_ordered
        )
    ]

    overall_values = [c.overall_value for c in children if c.overall_value is not None]
    class_mean = round(statistics.mean(overall_values)) if overall_values else None
    class_median = round(statistics.median(overall_values)) if overall_values else None

    # --- Mean PR per subtest, in definition (printed) order --------------
    subtest_rows: list[SubtestRow] = []
    for s in [s for s in definition.subtests if s.key not in component_keys]:
        values = [
            pr
            for pr in (child.subtest_pr.get(s.key) for child in children)
            if pr is not None
        ]
        mean_pr = round(statistics.mean(values), 1) if values else None
        category = None
        if mean_pr is not None:
            # The bar's colour follows the same classification basis as
            # the version's distribution (spec §6.5) — a subtest mean PR
            # only classifies meaningfully for a PR-classified version;
            # for a T-classified version the bar is still plotted (PRs are
            # what the sheet carries) but left uncoloured by category.
            if classification_basis == "pr":
                result = classify_fn(mean_pr)
                category = result.get("category")
        subtest_rows.append(
            SubtestRow(
                key=s.key,
                name=s.name,
                printed_label=s.printed_label,
                mean_pr=mean_pr,
                n_present=len(values),
                n_children=len(children),
                category=category,
            )
        )

    missing_data = MissingDataAudit(
        children_without_overall=[
            c.display_label for c in children if c.overall_value is None
        ],
        subtests_with_zero_present=[
            row.key for row in subtest_rows if row.n_present == 0
        ],
    )

    return ClassProfileData(
        klasse=klasse,
        schuljahr=schuljahr,
        test_version=test_version,
        test_key=definition.test_key,
        norm_test=definition.norm_test or "",
        test_form_summary=test_form_summary,
        test_date_min=test_date_min,
        test_date_max=test_date_max,
        child_count=len(children),
        awaiting_review_count=awaiting_review_count,
        classification_basis=classification_basis,
        categories_ordered=categories_ordered,
        norm_group=norm_group,
        norm_group_label=norm_group_label,
        norm_variant=norm_variant,
        cross_check_warning=cross_check_warning,
        children=children,
        distribution=distribution,
        class_mean=class_mean,
        class_median=class_median,
        class_n=len(overall_values),
        subtest_rows=subtest_rows,
        missing_data=missing_data,
        has_class_norms=definition.norm_test == "demat3plus",
        class_norm=class_norm,
    )
