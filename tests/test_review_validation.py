"""Unit tests for `demat.review_validation` (deterministic checks 1+2 fully;
check 3 with a stub runner, since it's the only LLM-calling piece).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from demat.agent_runner import RunnerUnavailableError
from demat.reports.norms import KnowledgeBaseUnavailableError
from demat.review_validation import (
    ReviewFinding,
    ReviewValidationResult,
    _check_norm_completion,
    _check_norm_mapping,
    _check_note_matches_mapping,
    _check_values_consistent,
)
from demat.test_registry import MetricDef, SubtestDef, SumRule, TestDefinition


def _definition() -> TestDefinition:
    return TestDefinition(
        test_key="demat2plus",
        test_version="DEMAT 2+",
        display_name="DEMAT 2+",
        extraction_skill="demat-2plus-extraction",
        norm_test="demat2plus",
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
                name="Zahleneigenschaften",
                rw_max=2,
                value_type="int",
                norm_scale="subtest_ZE",
                position=1,
            ),
            SubtestDef(
                key="AD",
                name="Addition",
                rw_max=4,
                value_type="int",
                norm_scale="subtest_AD",
                position=2,
            ),
        ],
        rules_sum=[SumRule(metric="rw_total", over="subtests.rw")],
    )


def _metric_row(
    key: str,
    value_num: float | None,
    value_raw: str | None = "x",
    *,
    value_text: str | None = None,
    value_provenance: str | None = None,
    position: int = 1,
) -> dict:
    return {
        "position": position,
        "metric_key": key,
        "value_raw": value_raw,
        "value_num": value_num,
        "value_text": value_text,
        "confidence": "high",
        "value_provenance": value_provenance,
    }


def _subtest_row(
    key: str,
    rw: float | None,
    rw_max: float | None,
    pr: int | None,
    *,
    pr_provenance: str | None = None,
    position: int = 1,
) -> dict:
    return {
        "position": position,
        "subtest_key": key,
        "printed_label": key,
        "name": key,
        "value_raw": str(rw),
        "rw": rw,
        "rw_max": rw_max,
        "pr": pr,
        "confidence": "high",
        "pr_provenance": pr_provenance,
    }


# --- check 1: values_consistent ------------------------------------------


def test_values_consistent_passes_when_sum_matches():
    definition = _definition()
    metrics_rows = [_metric_row("rw_total", 6.0)]
    subtest_rows = [
        _subtest_row("ZE", 2.0, 2, 90),
        _subtest_row("AD", 4.0, 4, 95),
    ]
    findings = _check_values_consistent(metrics_rows, subtest_rows, definition)
    assert findings == []


def test_values_consistent_flags_total_mismatch():
    definition = _definition()
    metrics_rows = [_metric_row("rw_total", 10.0)]
    subtest_rows = [_subtest_row("ZE", 2.0, 2, 90), _subtest_row("AD", 4.0, 4, 95)]
    findings = _check_values_consistent(metrics_rows, subtest_rows, definition)
    assert len(findings) == 1
    assert findings[0].severity == "error"
    assert "sum to 6.0" in findings[0].message


def test_values_consistent_flags_rw_exceeds_max():
    definition = _definition()
    metrics_rows = [_metric_row("rw_total", 5.0)]
    subtest_rows = [_subtest_row("ZE", 5.0, 2, 90)]
    findings = _check_values_consistent(metrics_rows, subtest_rows, definition)
    assert any("exceeds the maximum" in f.message for f in findings)


def test_values_consistent_flags_missing_required_metric():
    definition = _definition()
    findings = _check_values_consistent([], [], definition)
    assert any("Required metric rw_total" in f.message for f in findings)


# --- check 2: norm_mapping -------------------------------------------------


class _FakeNorms:
    def __init__(self, responses: dict[tuple[str, str], dict[str, Any]]):
        self.responses = responses
        self.calls: list[tuple[str, str, str, float]] = []

    def resolve_norm_group(self, norm_test, norm_variant):
        return "k3_gesamt", "Anfang 3. Klasse", "k3"

    def norm(self, norm_test, scale, group, raw, *, allow_flagged=False):
        self.calls.append((scale, group, raw))
        key = (scale, group)
        if key not in self.responses:
            raise RuntimeError(f"no fixture for {key}")
        return self.responses[key]


def test_norm_mapping_flags_pr_mismatch(monkeypatch):
    definition = _definition()
    fake = _FakeNorms(
        {
            ("subtest_ZE", "k3_jungen"): {"pr": 80, "status": "ok"},
        }
    )
    import demat.review_validation as rv

    monkeypatch.setattr(rv.norms, "resolve_norm_group", fake.resolve_norm_group)
    monkeypatch.setattr(rv.norms, "norm", fake.norm)

    assessment = {
        "test_date_iso": "2026-09-01",
        "gender": "junge",
        "norm_variant": "k3",
    }
    metrics_rows: list[dict] = []
    subtest_rows = [
        _subtest_row("ZE", 2.0, 2, 50)
    ]  # stored PR disagrees with fixture (80)
    findings = _check_norm_mapping(assessment, metrics_rows, subtest_rows, definition)
    errors = [f for f in findings if f.severity == "error"]
    assert len(errors) == 1
    assert "PR=50" in errors[0].message and "PR=80" in errors[0].message


def test_norm_mapping_passes_when_value_matches_either_candidate_group(monkeypatch):
    """A stored PR that matches the combined table but not the
    gender-specific one is not an error — which table a sheet was actually
    scored against is not recorded, and real data uses both."""
    definition = _definition()
    fake = _FakeNorms(
        {
            ("subtest_ZE", "k3_gesamt"): {"pr": 50, "status": "ok"},
            ("subtest_ZE", "k3_jungen"): {"pr": 80, "status": "ok"},
        }
    )
    import demat.review_validation as rv

    monkeypatch.setattr(rv.norms, "resolve_norm_group", fake.resolve_norm_group)
    monkeypatch.setattr(rv.norms, "norm", fake.norm)

    assessment = {
        "test_date_iso": "2026-09-01",
        "gender": "junge",
        "norm_variant": "k3",
    }
    subtest_rows = [_subtest_row("ZE", 2.0, 2, 50)]  # matches k3_gesamt, not k3_jungen
    findings = _check_norm_mapping(assessment, [], subtest_rows, definition)
    assert findings == []


def test_norm_mapping_flags_value_matching_neither_candidate_group(monkeypatch):
    definition = _definition()
    fake = _FakeNorms(
        {
            ("subtest_ZE", "k3_gesamt"): {"pr": 60, "status": "ok"},
            ("subtest_ZE", "k3_jungen"): {"pr": 80, "status": "ok"},
        }
    )
    import demat.review_validation as rv

    monkeypatch.setattr(rv.norms, "resolve_norm_group", fake.resolve_norm_group)
    monkeypatch.setattr(rv.norms, "norm", fake.norm)

    assessment = {
        "test_date_iso": "2026-09-01",
        "gender": "junge",
        "norm_variant": "k3",
    }
    subtest_rows = [_subtest_row("ZE", 2.0, 2, 50)]  # matches neither 60 nor 80
    findings = _check_norm_mapping(assessment, [], subtest_rows, definition)
    errors = [f for f in findings if f.severity == "error"]
    assert len(errors) == 1
    assert "k3_gesamt=60" in errors[0].message and "k3_jungen=80" in errors[0].message


def test_norm_mapping_downgrades_misprint_only_match_to_warning(monkeypatch):
    """A stored value that matches a candidate group's manual cell, but
    that cell is itself flagged as a probable misprint, is not a silent
    pass — it's a warning, not a hard error."""
    definition = _definition()
    fake = _FakeNorms(
        {
            ("subtest_ZE", "k3_gesamt"): {"pr": 60, "status": "ok"},
            ("subtest_ZE", "k3_jungen"): {
                "pr": 50,
                "status": "druckfehler_vermutet",
            },
        }
    )
    import demat.review_validation as rv

    monkeypatch.setattr(rv.norms, "resolve_norm_group", fake.resolve_norm_group)
    monkeypatch.setattr(rv.norms, "norm", fake.norm)

    assessment = {
        "test_date_iso": "2026-09-01",
        "gender": "junge",
        "norm_variant": "k3",
    }
    subtest_rows = [_subtest_row("ZE", 2.0, 2, 50)]  # matches only the flagged cell
    findings = _check_norm_mapping(assessment, [], subtest_rows, definition)
    assert len(findings) == 1
    assert findings[0].severity == "warning"
    assert "misprint" in findings[0].message


def test_norm_mapping_warns_on_unrecognized_gender(monkeypatch):
    definition = _definition()
    fake = _FakeNorms({})
    import demat.review_validation as rv

    monkeypatch.setattr(rv.norms, "resolve_norm_group", fake.resolve_norm_group)
    monkeypatch.setattr(rv.norms, "norm", fake.norm)

    assessment = {
        "test_date_iso": "2026-09-01",
        "gender": "unknown",
        "norm_variant": "k3",
    }
    findings = _check_norm_mapping(assessment, [], [], definition)
    assert any(
        f.severity == "warning" and "not recognized" in f.message for f in findings
    )
    assert fake.calls == []  # no rw values given, so no lookups happened


def test_norm_mapping_skips_when_kb_unavailable(monkeypatch):
    definition = _definition()
    import demat.review_validation as rv

    def _resolve(norm_test, norm_variant):
        return "k3_gesamt", "Anfang 3. Klasse", "k3"

    def _norm(norm_test, scale, group, raw, *, allow_flagged=False):
        raise KnowledgeBaseUnavailableError("no kb")

    monkeypatch.setattr(rv.norms, "resolve_norm_group", _resolve)
    monkeypatch.setattr(rv.norms, "norm", _norm)

    assessment = {
        "test_date_iso": "2026-09-01",
        "gender": "junge",
        "norm_variant": "k3",
    }
    subtest_rows = [_subtest_row("ZE", 2.0, 2, 50)]
    findings = _check_norm_mapping(assessment, [], subtest_rows, definition)
    assert all(f.severity == "warning" for f in findings)
    assert any("Knowledge base unavailable" in f.message for f in findings)


def test_norm_mapping_null_variant_warns_once_and_skips_lookups(monkeypatch):
    definition = _definition()
    fake = _FakeNorms({})
    import demat.review_validation as rv

    monkeypatch.setattr(rv.norms, "resolve_norm_group", fake.resolve_norm_group)
    monkeypatch.setattr(rv.norms, "norm", fake.norm)

    assessment = {"test_date_iso": "2026-09-01", "gender": "junge", "klasse": "3C"}
    subtest_rows = [_subtest_row("ZE", 2.0, 2, 50)]
    findings = _check_norm_mapping(assessment, [], subtest_rows, definition)
    assert len(findings) == 1
    assert findings[0].severity == "warning"
    assert fake.calls == []


def test_norm_mapping_cross_check_warning_appears_before_pr_mismatch_error(
    monkeypatch,
):
    definition = _definition()
    fake = _FakeNorms(
        {
            ("subtest_ZE", "k3_jungen"): {"pr": 80, "status": "ok"},
        }
    )
    import demat.review_validation as rv

    monkeypatch.setattr(rv.norms, "resolve_norm_group", fake.resolve_norm_group)
    monkeypatch.setattr(rv.norms, "norm", fake.norm)

    # DEMAT 2+'s "k3" variant expects grade 3; "4A" disagrees — the
    # cross-check warning must be appended before the per-value PR
    # mismatch below it explains.
    assessment = {
        "klasse": "4A",
        "gender": "junge",
        "norm_variant": "k3",
    }
    subtest_rows = [_subtest_row("ZE", 2.0, 2, 50)]  # disagrees with fixture (80)
    findings = _check_norm_mapping(assessment, [], subtest_rows, definition)
    assert len(findings) == 2
    assert findings[0].severity == "warning"
    assert "4A" in findings[0].message
    assert findings[1].severity == "error"


# --- check 3: note_matches_mapping -----------------------------------------


class _StubRunner:
    def __init__(self, matches: bool):
        self.matches = matches

    def run(self, *a, **k):
        raise NotImplementedError

    def run_text(
        self, prompt, output_path, output_model, skill_name=None, stop_event=None
    ):
        return {"matches": self.matches, "explanation": "stub"}


class _UnavailableRunner:
    def run(self, *a, **k):
        raise NotImplementedError

    def run_text(
        self, prompt, output_path, output_model, skill_name=None, stop_event=None
    ):
        raise RunnerUnavailableError("no cli")


def test_note_check_skips_without_note():
    definition = _definition()
    findings = _check_note_matches_mapping(
        {"assessment_note": ""}, [], definition, _StubRunner(True), Path("/tmp/x")
    )
    assert findings == []


def test_note_check_flags_disagreement(monkeypatch, tmp_path):
    definition = _definition()
    import demat.review_validation as rv

    monkeypatch.setattr(rv.norms, "basis_for", lambda d: "pr")
    monkeypatch.setattr(
        rv.norms, "classify", lambda *a, **k: {"category": "überdurchschnittlich"}
    )

    assessment = {"assessment_note": "Das Kind hatte große Schwierigkeiten."}
    metrics_rows = [_metric_row("pr_total", 90.0)]
    findings = _check_note_matches_mapping(
        assessment, metrics_rows, definition, _StubRunner(False), tmp_path
    )
    assert len(findings) == 1
    assert findings[0].severity == "error"


def test_note_check_warns_when_runner_unavailable(monkeypatch, tmp_path):
    definition = _definition()
    import demat.review_validation as rv

    monkeypatch.setattr(rv.norms, "basis_for", lambda d: "pr")
    monkeypatch.setattr(
        rv.norms, "classify", lambda *a, **k: {"category": "durchschnittlich"}
    )

    assessment = {"assessment_note": "Alles im Normbereich."}
    metrics_rows = [_metric_row("pr_total", 50.0)]
    findings = _check_note_matches_mapping(
        assessment, metrics_rows, definition, _UnavailableRunner(), tmp_path
    )
    assert len(findings) == 1
    assert findings[0].severity == "warning"


# --- check 4: norm_completion (ADR 0013) -------------------------------------


def _completion_assessment(norm_variant: str | None = "k3") -> dict:
    return {
        "assessment_id": 1,
        "klasse": "3c",
        "gender": "junge",
        "norm_variant": norm_variant,
    }


def test_norm_completion_info_per_computed_cell_names_source_I7():
    definition = _definition()
    subtest_rows = [
        _subtest_row("ZE", 2.0, 2, 20, pr_provenance="computed"),
        _subtest_row("AD", 4.0, 4, 90, pr_provenance="sheet"),
    ]
    metrics_rows = [
        _metric_row("rw_total", 6.0, "6", value_provenance="sheet"),
        _metric_row("pr_total", 10.0, "10", value_provenance="computed", position=2),
    ]
    findings = _check_norm_completion(
        _completion_assessment(), metrics_rows, subtest_rows, definition
    )
    infos = [f for f in findings if f.severity == "info"]
    assert len(infos) == 2
    ze = next(f for f in infos if f.field_ref == "subtest:ZE")
    assert "PR 20" in ze.message and "k3_gesamt" in ze.message
    pr_total = next(f for f in infos if f.field_ref == "metric:pr_total")
    assert "10" in pr_total.message and "k3_gesamt" in pr_total.message


def test_norm_completion_rw_total_computed_message_is_sum_not_lookup():
    definition = _definition()
    metrics_rows = [
        _metric_row("rw_total", 6.0, "6", value_provenance="computed"),
    ]
    findings = _check_norm_completion(
        _completion_assessment(), metrics_rows, [], definition
    )
    (finding,) = findings
    assert finding.field_ref == "metric:rw_total"
    assert "Summe der Subtest-RW" in finding.message


def test_norm_completion_no_variant_collapses_to_one_warning():
    definition = _definition()
    subtest_rows = [
        _subtest_row("ZE", 2.0, 2, None, pr_provenance="blank"),
        _subtest_row("AD", 4.0, 4, None, pr_provenance="blank"),
    ]
    findings = _check_norm_completion(
        _completion_assessment(norm_variant=None), [], subtest_rows, definition
    )
    warnings = [f for f in findings if f.severity == "warning"]
    assert len(warnings) == 1
    assert warnings[0].field_ref == "field:norm_variant"
    assert "2 Normwert" in warnings[0].message


def test_norm_completion_other_blank_cell_gets_per_cell_warning():
    definition = _definition()
    subtest_rows = [_subtest_row("ZE", None, 2, None, pr_provenance="blank")]
    findings = _check_norm_completion(
        _completion_assessment(), [], subtest_rows, definition
    )
    (finding,) = findings
    assert finding.severity == "warning"
    assert finding.field_ref == "subtest:ZE"


def test_norm_completion_unavailable_cell_produces_no_finding_I9():
    definition = _definition()
    subtest_rows = [_subtest_row("ZE", 0.0, 2, None, pr_provenance="unavailable")]
    metrics_rows = [_metric_row("pr_total", None, None, value_provenance="unavailable")]
    findings = _check_norm_completion(
        _completion_assessment(), metrics_rows, subtest_rows, definition
    )
    assert findings == []


def test_norm_completion_sheet_and_manual_cells_produce_no_finding():
    definition = _definition()
    subtest_rows = [_subtest_row("ZE", 2.0, 2, 20, pr_provenance="sheet")]
    metrics_rows = [_metric_row("pr_total", 15.0, "15", value_provenance="manual")]
    findings = _check_norm_completion(
        _completion_assessment(), metrics_rows, subtest_rows, definition
    )
    assert findings == []


def test_has_errors_ignores_info_findings():
    result = ReviewValidationResult(
        findings=[
            ReviewFinding(
                "norm_completion", "info", "computed", field_ref="subtest:ZE"
            ),
            ReviewFinding("norm_mapping", "warning", "no variant"),
        ]
    )
    assert result.has_errors is False


def test_has_errors_true_with_an_error_present():
    result = ReviewValidationResult(
        findings=[
            ReviewFinding(
                "norm_completion", "info", "computed", field_ref="subtest:ZE"
            ),
            ReviewFinding("norm_mapping", "error", "mismatch"),
        ]
    )
    assert result.has_errors is True


# --- _check_norm_mapping survives a non-numeric "≤1" KB/stored value --------


def test_norm_mapping_survives_le1_candidate(monkeypatch):
    definition = _definition()
    fake = _FakeNorms(
        {
            ("gesamtwert", "k3_gesamt"): {"pr": "≤1", "status": "ok"},
        }
    )
    import demat.review_validation as rv

    monkeypatch.setattr(rv.norms, "resolve_norm_group", fake.resolve_norm_group)
    monkeypatch.setattr(rv.norms, "norm", fake.norm)

    assessment = {
        "test_date_iso": "2026-09-01",
        "gender": "junge",
        "norm_variant": "k3",
    }
    metrics_rows = [
        _metric_row("rw_total", 6.0, "6"),
        _metric_row("pr_total", 10.0, "10", position=2),
    ]
    # Must not raise — the non-numeric "≤1" candidate is skipped, not compared.
    findings = _check_norm_mapping(assessment, metrics_rows, [], definition)
    assert findings == []


def test_norm_mapping_survives_stored_le1_metric(monkeypatch):
    definition = _definition()
    fake = _FakeNorms(
        {
            ("gesamtwert", "k3_gesamt"): {"pr": "≤1", "status": "ok"},
        }
    )
    import demat.review_validation as rv

    monkeypatch.setattr(rv.norms, "resolve_norm_group", fake.resolve_norm_group)
    monkeypatch.setattr(rv.norms, "norm", fake.norm)

    assessment = {
        "test_date_iso": "2026-09-01",
        "gender": "junge",
        "norm_variant": "k3",
    }
    metrics_rows = [
        _metric_row("rw_total", 2.0, "2"),
        _metric_row(
            "pr_total", None, "≤1", value_text="≤1", position=2
        ),  # stored verbatim, value_num=None
    ]
    findings = _check_norm_mapping(assessment, metrics_rows, [], definition)
    # Text-compare path: stored "≤1" matches the KB literal "≤1" exactly.
    assert findings == []
