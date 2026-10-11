"""ADR 0008 spec §6.1/§10: means over sparse `pr`, NULL handling, bucket
assignment on both classification bases (PR and T), the boundary
double-category rule, per-child sort order, and `STORE_RAW_NAMES=false`
labelling. Pure unit tests over `build_class_data` — no DB, no KB
subprocess (a fake `classify_fn` stands in for stage 3).
"""

from __future__ import annotations

from demat.reports.data import build_child_data, build_class_data, overall_metric_key
from demat.test_registry import MetricDef, SubtestDef, TestDefinition

_PR_BANDS = [
    (93, 100, "ausgezeichnet"),
    (71, 92, "überdurchschnittlich"),
    (30, 70, "durchschnittlich"),
    (15, 29, "unterdurchschnittlich"),
    (8, 14, "schwach"),
    (0, 7, "sehr schwach"),
]
_PR_CATEGORIES_ORDERED = [
    "sehr schwach",
    "schwach",
    "unterdurchschnittlich",
    "durchschnittlich",
    "überdurchschnittlich",
    "ausgezeichnet",
]

_T_BANDS = [  # fabricated stand-in bands — overlapping at 32/42/58/68
    (68, 999, "weit überdurchschnittlich"),
    (58, 68, "überdurchschnittlich"),
    (42, 58, "durchschnittlich"),
    (32, 42, "unterdurchschnittlich"),
    (-999, 32, "weit unterdurchschnittlich"),
]
_T_CATEGORIES_ORDERED = [
    "weit unterdurchschnittlich",
    "unterdurchschnittlich",
    "durchschnittlich",
    "überdurchschnittlich",
    "weit überdurchschnittlich",
]


def _fake_classify_pr(value: float) -> dict:
    hits = [name for lo, hi, name in _PR_BANDS if lo <= value <= hi]
    out: dict = {"category": hits[0] if len(hits) == 1 else None, "basis": "pr"}
    if len(hits) > 1:
        out["categories"] = hits
    return out


def _fake_classify_t(value: float) -> dict:
    hits = [name for lo, hi, name in _T_BANDS if lo <= value <= hi]
    out: dict = {"category": hits[0] if len(hits) == 1 else None, "basis": "t"}
    if len(hits) > 1:
        out["categories"] = hits
    return out


def _pr_definition() -> TestDefinition:
    return TestDefinition(
        test_key="demat2plus",
        test_version="DEMAT 2+",
        display_name="DEMAT 2+",
        extraction_skill="demat-2plus-extraction",
        norm_test="demat2plus",
        overall_metrics=[
            MetricDef(key="rw_total", label="Rohwert", value_type="real"),
            MetricDef(
                key="pr_total",
                label="Prozentrang",
                value_type="int",
                norm_scale="gesamtwert",
                norm_field="pr",
            ),
            MetricDef(
                key="t_total",
                label="T-Wert",
                value_type="real",
                norm_scale="gesamtwert",
                norm_field="t_wert",
            ),
        ],
        subtests=[
            SubtestDef(key="ZE", name="Zahleneigenschaften", rw_max=2),
            SubtestDef(key="AD", name="Addition", rw_max=4),
        ],
    )


def _t_definition() -> TestDefinition:
    return TestDefinition(
        test_key="demat5plus",
        test_version="DEMAT 5+",
        display_name="DEMAT 5+",
        extraction_skill="demat-5plus-extraction",
        norm_test="demat5plus",
        overall_metrics=[
            MetricDef(
                key="pr_total",
                label="Prozentrang",
                value_type="int",
                norm_scale="gesamtwert",
                norm_field="pr",
            ),
            MetricDef(
                key="t_total",
                label="T-Wert",
                value_type="real",
                norm_scale="gesamtwert",
                norm_field="t_wert",
            ),
        ],
        subtests=[
            SubtestDef(key="A", name="Arithmetik", rw_max=14),
        ],
    )


def _row(assessment_id, person_id=None):
    return {"assessment_id": assessment_id, "person_id": person_id}


def _metric(key, value_num):
    return {"metric_key": key, "value_num": value_num, "value_raw": str(value_num)}


def _subtest(key, pr, rw=None):
    return {"subtest_key": key, "pr": pr, "rw": rw}


def _build(
    *,
    definition,
    classification_basis,
    classify_fn,
    assessment_rows,
    metrics_by_assessment,
    subtests_by_assessment,
    raw_names_by_assessment=None,
    store_raw_names=True,
):
    return build_class_data(
        klasse="3C",
        schuljahr="2026-27",
        test_version=definition.test_version,
        test_form_summary="A",
        test_date_min="2026-09-16",
        test_date_max="2026-09-16",
        awaiting_review_count=0,
        assessment_rows=assessment_rows,
        metrics_by_assessment=metrics_by_assessment,
        subtests_by_assessment=subtests_by_assessment,
        raw_names_by_assessment=raw_names_by_assessment or {},
        store_raw_names=store_raw_names,
        definition=definition,
        classification_basis=classification_basis,
        classify_fn=classify_fn,
        norm_group="k3_gesamt",
        norm_group_label="Anfang 3. Klasse",
        norm_variant="k3",
        cross_check_warning=None,
    )


# --- means over sparse pr / NULL handling -----------------------------------


def test_mean_pr_over_sparse_subtest_scores():
    definition = _pr_definition()
    rows = [_row(1, "AAAA"), _row(2, "BBBB"), _row(3, "CCCC")]
    metrics = {
        1: [_metric("pr_total", 80)],
        2: [_metric("pr_total", 60)],
        3: [_metric("pr_total", 40)],
    }
    subtests = {
        1: [_subtest("ZE", 90), _subtest("AD", 50)],
        2: [_subtest("ZE", 70)],  # AD missing entirely for this child
        3: [_subtest("ZE", None), _subtest("AD", 30)],  # ZE transcribed as null
    }

    data = _build(
        definition=definition,
        classification_basis="pr",
        classify_fn=_fake_classify_pr,
        assessment_rows=rows,
        metrics_by_assessment=metrics,
        subtests_by_assessment=subtests,
    )

    ze_row = next(r for r in data.subtest_rows if r.key == "ZE")
    ad_row = next(r for r in data.subtest_rows if r.key == "AD")

    assert ze_row.n_present == 2  # child 3's NULL pr excluded
    assert ze_row.n_children == 3
    assert ze_row.mean_pr == 80.0  # mean(90, 70)

    assert ad_row.n_present == 2  # child 2 never carries an AD row at all
    assert ad_row.mean_pr == 40.0  # mean(50, 30)


def test_children_without_overall_value_are_excluded_from_class_mean():
    definition = _pr_definition()
    rows = [_row(1, "AAAA"), _row(2, "BBBB")]
    metrics = {1: [_metric("pr_total", 80)], 2: []}  # child 2 has no pr_total at all
    subtests: dict = {1: [], 2: []}

    data = _build(
        definition=definition,
        classification_basis="pr",
        classify_fn=_fake_classify_pr,
        assessment_rows=rows,
        metrics_by_assessment=metrics,
        subtests_by_assessment=subtests,
    )

    assert data.class_n == 1
    assert data.class_mean == 80
    assert data.missing_data.children_without_overall == ["BBBB"]


# --- bucket assignment on both bases -----------------------------------------


def test_bucket_assignment_pr_basis():
    definition = _pr_definition()
    rows = [_row(1, "AAAA"), _row(2, "BBBB")]
    metrics = {1: [_metric("pr_total", 98)], 2: [_metric("pr_total", 20)]}
    subtests: dict = {1: [], 2: []}

    data = _build(
        definition=definition,
        classification_basis="pr",
        classify_fn=_fake_classify_pr,
        assessment_rows=rows,
        metrics_by_assessment=metrics,
        subtests_by_assessment=subtests,
    )

    by_category = {b.category: b.count for b in data.distribution}
    assert by_category["ausgezeichnet"] == 1
    assert by_category["unterdurchschnittlich"] == 1
    # All six manual categories present, in manual order, not the sample's 4.
    assert [b.category for b in data.distribution] == _PR_CATEGORIES_ORDERED


def test_bucket_assignment_t_basis():
    definition = _t_definition()
    rows = [_row(1, "AAAA"), _row(2, "BBBB")]
    metrics = {1: [_metric("t_total", 75.0)], 2: [_metric("t_total", 25.0)]}
    subtests: dict = {1: [], 2: []}

    data = _build(
        definition=definition,
        classification_basis="t",
        classify_fn=_fake_classify_t,
        assessment_rows=rows,
        metrics_by_assessment=metrics,
        subtests_by_assessment=subtests,
    )

    by_category = {b.category: b.count for b in data.distribution}
    assert by_category["weit überdurchschnittlich"] == 1
    assert by_category["weit unterdurchschnittlich"] == 1
    assert [b.category for b in data.distribution] == _T_CATEGORIES_ORDERED


# --- boundary double-category -----------------------------------------------


def test_boundary_value_counts_once_in_the_lower_category():
    """DEMAT 5+ T=32/42/58/68 are overlapping boundaries — `classify()`
    returns two categories; the child is counted once, in the worse one."""
    definition = _t_definition()
    rows = [_row(1, "AAAA")]
    metrics = {1: [_metric("t_total", 32.0)]}  # boundary: unter/weit unter
    subtests: dict = {1: []}

    data = _build(
        definition=definition,
        classification_basis="t",
        classify_fn=_fake_classify_t,
        assessment_rows=rows,
        metrics_by_assessment=metrics,
        subtests_by_assessment=subtests,
    )

    child = data.children[0]
    assert child.overall_category is None
    assert set(child.overall_categories) == {
        "unterdurchschnittlich",
        "weit unterdurchschnittlich",
    }
    by_category = {b.category: b.count for b in data.distribution}
    # Counted once, in the lower (worse) of the two categories.
    assert by_category["weit unterdurchschnittlich"] == 1
    assert by_category["unterdurchschnittlich"] == 0


# --- sort order (render.py's _sorted_children) -------------------------------


def test_render_sorts_children_by_overall_value_descending_null_last():
    from demat.reports.render import _sorted_children

    definition = _pr_definition()
    rows = [_row(1, "AAAA"), _row(2, "BBBB"), _row(3, "CCCC")]
    metrics = {
        1: [_metric("pr_total", 40)],
        2: [_metric("pr_total", 90)],
        3: [],  # no overall value
    }
    subtests: dict = {1: [], 2: [], 3: []}

    data = _build(
        definition=definition,
        classification_basis="pr",
        classify_fn=_fake_classify_pr,
        assessment_rows=rows,
        metrics_by_assessment=metrics,
        subtests_by_assessment=subtests,
    )

    ordered = _sorted_children(data)
    assert [c.person_id for c in ordered] == ["BBBB", "AAAA", "CCCC"]


# --- STORE_RAW_NAMES=false labelling -----------------------------------------


def test_store_raw_names_false_uses_person_id_label():
    definition = _pr_definition()
    rows = [_row(1, "AAAA")]
    metrics = {1: [_metric("pr_total", 50)]}
    subtests: dict = {1: []}

    data = _build(
        definition=definition,
        classification_basis="pr",
        classify_fn=_fake_classify_pr,
        assessment_rows=rows,
        metrics_by_assessment=metrics,
        subtests_by_assessment=subtests,
        raw_names_by_assessment={1: "Max Mustermann"},
        store_raw_names=False,
    )

    assert data.children[0].display_label == "AAAA"


def test_store_raw_names_true_uses_raw_name_label():
    definition = _pr_definition()
    rows = [_row(1, "AAAA")]
    metrics = {1: [_metric("pr_total", 50)]}
    subtests: dict = {1: []}

    data = _build(
        definition=definition,
        classification_basis="pr",
        classify_fn=_fake_classify_pr,
        assessment_rows=rows,
        metrics_by_assessment=metrics,
        subtests_by_assessment=subtests,
        raw_names_by_assessment={1: "Max Mustermann"},
        store_raw_names=True,
    )

    assert data.children[0].display_label == "Max Mustermann"


def test_unlinked_child_gets_synthetic_person_ref_and_label():
    definition = _pr_definition()
    rows = [_row(assessment_id=7, person_id=None)]
    metrics = {7: [_metric("pr_total", 50)]}
    subtests: dict = {7: []}

    data = _build(
        definition=definition,
        classification_basis="pr",
        classify_fn=_fake_classify_pr,
        assessment_rows=rows,
        metrics_by_assessment=metrics,
        subtests_by_assessment=subtests,
    )

    child = data.children[0]
    assert child.person_ref == "unlinked-7"
    assert child.display_label == "unlinked #7"


# --- build_child_data() (ADR 0014 spec §11: extracted, no behaviour change) --


def test_build_child_data_matches_build_class_data_for_the_same_row():
    """`build_child_data()` (extracted for the child-report pipeline) must
    produce the identical `ChildData` `build_class_data()` builds inline
    for the same single-row input — this is the "no behaviour change"
    guarantee spec §11 requires of the extraction.
    """
    definition = _pr_definition()
    overall_key = overall_metric_key(definition, "pr")
    rows = [_row(assessment_id=42, person_id="ABCD")]
    metrics = {42: [_metric("pr_total", 80)]}
    subtests = {42: [_subtest("ZE", pr=90, rw=2), _subtest("AD", pr=70, rw=3)]}

    via_class = _build(
        definition=definition,
        classification_basis="pr",
        classify_fn=_fake_classify_pr,
        assessment_rows=rows,
        metrics_by_assessment=metrics,
        subtests_by_assessment=subtests,
    ).children[0]

    via_child = build_child_data(
        assessment_id=42,
        person_id="ABCD",
        raw_name=None,
        store_raw_names=True,
        overall_key=overall_key,
        metrics=metrics[42],
        subtests=subtests[42],
        classification_basis="pr",
        categories_ordered=_PR_CATEGORIES_ORDERED,
        classify_fn=_fake_classify_pr,
        component_keys=definition.component_keys(),
    )

    assert via_child == via_class


def test_overall_metric_key_raises_when_basis_unmatched():
    definition = _pr_definition()
    definition.overall_metrics = [
        m for m in definition.overall_metrics if m.norm_field != "t_wert"
    ]
    try:
        overall_metric_key(definition, "t")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError")
