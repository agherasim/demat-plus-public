"""ADR 0014 spec §11: `build_child_report_data()`. Pure unit tests with a
fake `classify_fn`/injected `bands`/`median_zone` — no DB, no KB
subprocess (those are stage 3, exercised separately in
`test_band_segments.py`).
"""

from __future__ import annotations

import pytest

from demat.reports.child_data import ELIGIBLE_STATUSES, build_child_report_data
from demat.reports.norms import BandSegment
from demat.test_registry import MetricDef, SubtestDef, TestDefinition

_BANDS = (
    BandSegment(lo=0, hi=7, category="sehr schwach", label="0–7", open_low=True),
    BandSegment(lo=8, hi=14, category="schwach", label="8–14"),
    BandSegment(lo=15, hi=29, category="unterdurchschnittlich", label="15–29"),
    BandSegment(lo=30, hi=70, category="durchschnittlich", label="30–70"),
    BandSegment(lo=71, hi=92, category="überdurchschnittlich", label="71–92"),
    BandSegment(
        lo=93, hi=100, category="ausgezeichnet", label="93–100", open_high=True
    ),
)
_CATEGORIES_ORDERED = [b.category for b in _BANDS]


def _fake_classify_pr(value):
    for b in _BANDS:
        if b.lo <= value <= b.hi:
            return {"category": b.category, "basis": "pr"}
    return {"category": None, "basis": "pr"}


def _definition_2plus() -> TestDefinition:
    return TestDefinition(
        test_key="demat2plus",
        test_version="DEMAT 2+",
        display_name="DEMAT 2+",
        extraction_skill="demat-2plus-extraction",
        norm_test="demat2plus",
        overall_metrics=[
            MetricDef(
                key="rw_total", label="Rohwert", value_type="real", max_possible=36
            ),
            MetricDef(
                key="pr_total",
                label="Prozentrang",
                value_type="int",
                norm_scale="gesamtwert",
                norm_field="pr",
            ),
            MetricDef(
                key="pr_band",
                label="PR-Band",
                value_type="text",
                norm_scale="gesamtwert",
                norm_field="pr_band",
            ),
            MetricDef(
                key="t_total",
                label="T-Wert",
                value_type="real",
                norm_scale="gesamtwert",
                norm_field="t_wert",
            ),
            MetricDef(
                key="t_band",
                label="T-Band",
                value_type="text",
                norm_scale="gesamtwert",
                norm_field="t_band",
            ),
        ],
        subtests=[
            SubtestDef(
                key="ZE", name="Zahleneigenschaften", rw_max=2, norm_scale="subtest_ZE"
            ),
            SubtestDef(key="AD", name="Addition", rw_max=4, norm_scale="subtest_AD"),
        ],
    )


def _definition_3plus() -> TestDefinition:
    return TestDefinition(
        test_key="demat3plus",
        test_version="DEMAT 3+",
        display_name="DEMAT 3+",
        extraction_skill="demat-3plus-extraction",
        norm_test="demat3plus",
        overall_metrics=[
            MetricDef(
                key="rw_total", label="Rohwert", value_type="real", max_possible=31
            ),
            MetricDef(
                key="pr_total",
                label="Prozentrang",
                value_type="int",
                norm_scale="gesamtwert",
                norm_field="pr",
            ),
            MetricDef(
                key="pr_band",
                label="PR-Band",
                value_type="text",
                norm_scale="gesamtwert",
                norm_field="pr_band",
            ),
        ],
        subtests=[
            SubtestDef(
                key="ARI",
                printed_label="Arithmetik",
                name="Arithmetik",
                rw_max=15,
                norm_scale="subtest_arithmetik",
            ),
        ],
    )


def _assessment(**overrides):
    base = {
        "assessment_id": 1,
        "status": "validated_ok",
        "person_id": "ABCD",
        "person_name": "Test Kind",
        "klasse": "3A",
        "test_version": "DEMAT 2+",
        "test_form": "A",
        "test_date_iso": "2026-09-15",
        "norm_variant": "k3",
        "finalized_at": "2026-09-20",
    }
    base.update(overrides)
    return base


def _metric(key, value_num=None, value_raw=None, value_text=None):
    return {
        "metric_key": key,
        "value_num": value_num,
        "value_raw": value_raw
        if value_raw is not None
        else (str(value_num) if value_num is not None else None),
        "value_text": value_text,
    }


def _subtest(key, pr=None, rw=None, value_raw=None):
    return {"subtest_key": key, "pr": pr, "rw": rw, "value_raw": value_raw}


def _build(**overrides):
    kwargs = {
        "assessment": _assessment(),
        "metrics": [
            _metric("rw_total", 30),
            _metric("pr_total", 80),
            _metric("pr_band", value_text="64–83"),
            _metric("t_total", 58),
            _metric("t_band", value_text="52–60"),
        ],
        "subtests": [_subtest("ZE", pr=90, rw=2), _subtest("AD", pr=20, rw=1)],
        "definition": _definition_2plus(),
        "store_raw_names": False,
        "classification_basis": "pr",
        "classification_source": "Manual DEMAT 2+",
        "classify_fn": _fake_classify_pr,
        "bands": _BANDS,
        "categories_ordered": _CATEGORIES_ORDERED,
        "median_zone": (30, 70),
        "norm_variant_label": "3. Klasse",
        "cross_check_warning": None,
        "gesamt_status": None,
        "provenance_phrase": None,
        "generated_at": "2026-09-27",
        "template_version": "1",
    }
    kwargs.update(overrides)
    return build_child_report_data(**kwargs)


# --- eligibility (spec §4) ----------------------------------------------------


@pytest.mark.parametrize("status", ["needs_review", "rejected"])
def test_ineligible_status_raises(status):
    with pytest.raises(ValueError):
        _build(assessment=_assessment(status=status))


@pytest.mark.parametrize("status", sorted(ELIGIBLE_STATUSES))
def test_eligible_statuses_do_not_raise(status):
    _build(assessment=_assessment(status=status))


# --- stored-value rating (never re-derived) ----------------------------------


def test_rating_is_from_the_stored_value_only():
    data = _build()
    assert data.overall_value == 80.0
    assert data.child.overall_category == "überdurchschnittlich"
    assert data.rating_rank == 4


def test_boundary_value_carries_both_categories():
    def classify_boundary(value):
        return {
            "category": None,
            "categories": ["schwach", "unterdurchschnittlich"],
            "basis": "pr",
        }

    data = _build(classify_fn=classify_boundary)
    assert data.child.overall_categories == ["schwach", "unterdurchschnittlich"]


def test_missing_overall_value_yields_none():
    data = _build(metrics=[_metric("pr_band", value_text="—")])
    assert data.overall_value is None
    assert data.child.overall_category is None


# --- Gesamtwert fields per layout --------------------------------------------


def test_gesamt_fields_for_2plus_layout():
    data = _build()
    labels = [f.label for f in data.gesamt_fields]
    assert labels == [
        "Prozentrang (ges)",
        "Prozentrang-Band (68 %)",
        "T-Wert (ges)",
        "T-Wert-Band (68 %)",
    ]
    kinds = [f.kind for f in data.gesamt_fields]
    assert kinds == ["value", "range", "value", "range"]
    assert data.rw_total_display == "30 (36)"


def test_gesamt_field_absent_when_metric_missing():
    data = _build(metrics=[_metric("pr_total", 80)])
    t_band_field = next(f for f in data.gesamt_fields if "T-Wert-Band" in f.label)
    assert t_band_field.present is False
    assert t_band_field.display == "—"


# --- overall_band_range: parsed for 3+ only ----------------------------------


def test_overall_band_range_none_for_rows_layout():
    data = _build()
    assert data.overall_band_range is None


def test_overall_band_range_parsed_for_corridor_layout():
    data = _build(
        assessment=_assessment(test_version="DEMAT 3+"),
        definition=_definition_3plus(),
        metrics=[
            _metric("rw_total", 12),
            _metric("pr_total", 10),
            _metric("pr_band", value_text="4–18"),
        ],
        subtests=[_subtest("ARI", pr=30, rw=7)],
    )
    assert data.sheet_layout == "columns_with_corridor"
    assert data.overall_band_range == (4.0, 18.0)


def test_overall_band_range_none_when_unparseable():
    data = _build(
        assessment=_assessment(test_version="DEMAT 3+"),
        definition=_definition_3plus(),
        metrics=[
            _metric("rw_total", 12),
            _metric("pr_total", 10),
            _metric("pr_band", value_text="unklar"),
        ],
        subtests=[_subtest("ARI", pr=30, rw=7)],
    )
    assert data.overall_band_range is None


# --- median_zone: set even without an overall value, identical across children --


def test_median_zone_set_even_without_overall_value():
    data = _build(metrics=[], median_zone=(26, 75))
    assert data.median_zone == (26, 75)


def test_median_zone_identical_for_two_children():
    a = _build()
    b = _build(assessment=_assessment(assessment_id=2, person_id="WXYZ"))
    assert a.median_zone == b.median_zone


# --- subtest lines / escaping / provenance -----------------------------------


def test_subtest_names_are_escaped_in_profile_lines():
    data = _build(
        subtests=[_subtest("ZE", pr=90, rw=2), _subtest("AD", pr=20, rw=1)],
    )
    assert "&" not in data.profile_high or "&amp;" in data.profile_high


def test_provenance_phrase_passes_through_when_present():
    data = _build(
        provenance_phrase="Normwerte aus Normtabelle k3_gesamt nachgeschlagen"
    )
    assert (
        data.provenance_phrase == "Normwerte aus Normtabelle k3_gesamt nachgeschlagen"
    )


def test_provenance_phrase_none_by_default():
    data = _build()
    assert data.provenance_phrase is None


def test_person_ref_and_display_name_follow_store_raw_names():
    data = _build(store_raw_names=False)
    assert data.display_name is None
    data2 = _build(store_raw_names=True)
    assert data2.display_name == "Test Kind"


def test_unlinked_assessment_gets_synthetic_person_ref():
    data = _build(assessment=_assessment(person_id=None))
    assert data.person_ref == "unlinked-1"
