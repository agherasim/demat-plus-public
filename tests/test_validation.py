"""Unit tests for `demat.validation` (ADR 0005 §6: definition-driven,
tolerance-based float comparisons).
"""

from __future__ import annotations

from datetime import date

from demat.schema import ExtractedSheet, MetricValue, SubtestScore, TriageResult
from demat.test_registry import MetricDef, SubtestDef, SumRule, TestDefinition
from demat.validation import coerce_float, flag_severity, is_blocking_flag, validate

TODAY = date(2026, 9, 20)


def _definition() -> TestDefinition:
    return TestDefinition(
        test_key="demat2plus",
        test_version="DEMAT 2+",
        display_name="DEMAT 2+",
        extraction_skill="demat-2plus-extraction",
        overall_metrics=[
            MetricDef(
                key="rw_total",
                label="Rohwert gesamt",
                value_type="real",
                min_value=0,
                max_possible=36,
                required=True,
                norm_scale="gesamtwert",
                position=1,
            ),
            MetricDef(
                key="pr_total",
                label="Prozentrang gesamt",
                value_type="int",
                min_value=0,
                max_value=100,
                norm_scale="gesamtwert",
                norm_field="pr",
                position=2,
            ),
            MetricDef(
                key="t_total",
                label="T-Wert",
                value_type="real",
                min_value=20,
                max_value=80,
                norm_scale="gesamtwert",
                norm_field="t_wert",
                position=3,
            ),
        ],
        subtests=[
            SubtestDef(
                key="ZE",
                printed_label="ZE",
                name="Zahleneigenschaften",
                rw_max=2,
                value_type="int",
                norm_scale="subtest_ZE",
                position=1,
            ),
            SubtestDef(
                key="SA",
                printed_label="SA",
                name="Sachaufgaben",
                rw_max=4,
                value_type="real",
                norm_scale="subtest_SA",
                position=2,
            ),
        ],
        rules_sum=[SumRule(metric="rw_total", over="subtests.rw")],
    )


def _base_sheet(**overrides) -> ExtractedSheet:
    defaults = {
        "is_demat_sheet": True,
        "test_version": "DEMAT 2+",
        "test_form": "A",
        "child_name": "Max Mustermann",
        "child_name_confidence": "high",
        "klasse": "3c",
        "gender": "junge",
        "test_date_raw": "16.09.26",
        "test_date_iso": "2026-09-16",
        "test_date_confidence": "high",
        "subtests": [],
        "overall": [],
        "assessment_note": None,
        "illegible_notes": None,
        "overall_confidence": "high",
    }
    defaults.update(overrides)
    return ExtractedSheet(**defaults)


def _triage() -> TriageResult:
    return TriageResult(
        is_demat_sheet=True, test_version="DEMAT 2+", test_form="A", confidence="high"
    )


def test_coerce_float_accepts_german_decimal_comma():
    assert coerce_float("3,5") == 3.5
    assert coerce_float("20,5") == 20.5
    assert coerce_float("13") == 13.0
    assert coerce_float(None) is None
    assert coerce_float("") is None
    assert coerce_float("not a number") is None


def test_sum_rule_uses_tolerance_not_equality():
    sheet = _base_sheet(
        subtests=[
            SubtestScore(key="ZE", rw="2", rw_max="2", confidence="high"),
            SubtestScore(key="SA", rw="3,5", rw_max="4", confidence="high"),
        ],
        overall=[MetricValue(key="rw_total", value="5,5", confidence="high")],
    )
    result = validate(sheet, _triage(), _definition(), today=TODAY)
    assert "total_mismatch" not in result.flags


def test_sum_rule_flags_a_real_mismatch():
    sheet = _base_sheet(
        subtests=[
            SubtestScore(key="ZE", rw="2", rw_max="2", confidence="high"),
            SubtestScore(key="SA", rw="3,5", rw_max="4", confidence="high"),
        ],
        overall=[MetricValue(key="rw_total", value="99", confidence="high")],
    )
    result = validate(sheet, _triage(), _definition(), today=TODAY)
    assert "total_mismatch" in result.flags


def test_half_point_subtest_does_not_spuriously_exceed_max():
    sheet = _base_sheet(
        subtests=[SubtestScore(key="SA", rw="3,5", rw_max="4", confidence="high")],
        overall=[],
    )
    result = validate(sheet, _triage(), _definition(), today=TODAY)
    assert "rw_exceeds_max" not in result.flags


def test_rw_exceeds_max_flags():
    sheet = _base_sheet(
        subtests=[SubtestScore(key="ZE", rw="5", rw_max="2", confidence="high")],
        overall=[],
    )
    result = validate(sheet, _triage(), _definition(), today=TODAY)
    assert "rw_exceeds_max" in result.flags


def test_metric_out_of_range_flags():
    sheet = _base_sheet(
        overall=[MetricValue(key="pr_total", value="150", confidence="high")]
    )
    result = validate(sheet, _triage(), _definition(), today=TODAY)
    assert "metric_out_of_range" in result.flags


def test_unknown_metric_is_warning_only():
    sheet = _base_sheet(
        overall=[
            MetricValue(key="rw_total", value="10", confidence="high"),
            MetricValue(key="not_a_real_metric", value="1", confidence="high"),
        ]
    )
    result = validate(sheet, _triage(), _definition(), today=TODAY)
    assert "unknown_metric" in result.flags
    assert result.has_blocking_flags is False


def test_missing_required_metric_flags_and_forces_review():
    sheet = _base_sheet(overall=[])
    result = validate(sheet, _triage(), _definition(), today=TODAY)
    assert "missing_required_metric" in result.flags
    assert result.has_blocking_flags is True


def test_duplicate_metric_flags():
    sheet = _base_sheet(
        overall=[
            MetricValue(key="rw_total", value="10", confidence="high"),
            MetricValue(key="rw_total", value="11", confidence="high"),
        ]
    )
    result = validate(sheet, _triage(), _definition(), today=TODAY)
    assert "duplicate_metric" in result.flags


def test_metric_unparseable_does_not_raise():
    sheet = _base_sheet(
        overall=[MetricValue(key="rw_total", value="not-a-number", confidence="high")]
    )
    result = validate(sheet, _triage(), _definition(), today=TODAY)
    assert "metric_unparseable" in result.flags


def test_validate_with_none_definition_skips_definition_driven_checks():
    sheet = _base_sheet(
        overall=[MetricValue(key="rw_total", value="10", confidence="high")]
    )
    result = validate(sheet, _triage(), None, today=TODAY)
    assert "unknown_metric" not in result.flags
    assert "missing_required_metric" not in result.flags


# --- flag_severity / is_blocking_flag (ADR 0013) ----------------------------


def test_flag_severity_norm_values_computed_is_info():
    assert flag_severity("norm_values_computed") == "info"


def test_flag_severity_warning_only_flag_is_warning():
    assert flag_severity("medium_confidence") == "warning"
    assert flag_severity("unknown_subtest") == "warning"


def test_flag_severity_other_flag_is_error():
    assert flag_severity("missing_required_metric") == "error"
    assert flag_severity("some_future_unrecognized_flag") == "error"


def test_is_blocking_flag_true_only_for_error_severity():
    assert is_blocking_flag("missing_required_metric") is True
    assert is_blocking_flag("medium_confidence") is False
    assert is_blocking_flag("norm_values_computed") is False


def test_has_blocking_flags_ignores_info_and_warning():
    sheet = _base_sheet(
        overall=[MetricValue(key="rw_total", value="10", confidence="medium")]
    )
    result = validate(sheet, _triage(), None, today=TODAY)
    assert "medium_confidence" in result.flags
    assert result.has_blocking_flags is False
