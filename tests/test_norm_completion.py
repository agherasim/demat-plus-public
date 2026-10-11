"""Unit tests for `demat.norm_completion.plan_completion` (ADR 0013). Pure:
`lookup` is a fake, and no knowledge base is touched. Values are modelled on
the KB survey in the spec but are not copied verbatim from any real sheet
(only the sample's RW numbers are ever used elsewhere, per the spec's data
protection rule — this file uses fabricated RWs instead).
"""

from __future__ import annotations

from typing import Any

from demat.norm_completion import plan_completion
from demat.reports.norms import KnowledgeBaseUnavailableError
from demat.test_registry import MetricDef, SubtestDef, SumRule, TestDefinition


def _definition(**overrides: Any) -> TestDefinition:
    defaults: dict[str, Any] = {
        "test_key": "demat2plus",
        "test_version": "DEMAT 2+",
        "display_name": "DEMAT 2+",
        "extraction_skill": "demat-2plus-extraction",
        "norm_test": "demat2plus",
        "overall_metrics": [
            MetricDef(
                key="rw_total",
                label="Rohwert gesamt",
                value_type="real",
                required=True,
                norm_scale="gesamtwert",
                position=1,
            ),
            MetricDef(
                key="pr_total",
                label="Prozentrang gesamt",
                value_type="int",
                norm_scale="gesamtwert",
                norm_field="pr",
                position=2,
            ),
            MetricDef(
                key="pr_band",
                label="Prozentrang-Bereich",
                value_type="text",
                norm_scale="gesamtwert",
                norm_field="pr_band",
                position=3,
            ),
            MetricDef(
                key="t_total",
                label="T-Wert",
                value_type="real",
                norm_scale="gesamtwert",
                norm_field="t_wert",
                position=4,
            ),
        ],
        "subtests": [
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
        "rules_sum": [SumRule(metric="rw_total", over="subtests.rw")],
    }
    defaults.update(overrides)
    return TestDefinition(**defaults)


def _assessment(*, status="needs_review", norm_variant="k3") -> dict[str, Any]:
    return {
        "assessment_id": 1,
        "status": status,
        "norm_variant": norm_variant,
        "klasse": "3c",
        "gender": "junge",
    }


def _metric_row(
    key: str,
    *,
    position: int = 1,
    value_raw: str | None = None,
    value_num: float | None = None,
    value_text: str | None = None,
    provenance: str | None = "blank",
) -> dict[str, Any]:
    return {
        "position": position,
        "metric_key": key,
        "value_raw": value_raw,
        "value_num": value_num,
        "value_text": value_text,
        "confidence": "high",
        "value_provenance": provenance,
    }


def _subtest_row(
    key: str,
    *,
    position: int = 1,
    rw: float | None = None,
    rw_max: float | None = None,
    pr: int | None = None,
    provenance: str | None = "blank",
) -> dict[str, Any]:
    return {
        "position": position,
        "subtest_key": key,
        "printed_label": key,
        "name": key,
        "value_raw": str(rw) if rw is not None else None,
        "rw": rw,
        "rw_max": rw_max,
        "pr": pr,
        "confidence": "high",
        "pr_provenance": provenance,
    }


def _fake_lookup(table: dict[tuple[str, str, str, float], dict[str, Any]]):
    def lookup(norm_test: str, scale: str, group: str, raw: float) -> dict[str, Any]:
        key = (norm_test, scale, group, raw)
        if key not in table:
            raise RuntimeError(f"no fake row for {key}")
        return table[key]

    return lookup


# --- gating (I1, I2) -------------------------------------------------------


def test_no_definition_returns_empty_plan():
    plan = plan_completion(_assessment(), [], [], None, lookup=lambda *a: {})
    assert plan.writes == []
    assert plan.skipped == []


def test_no_norm_test_returns_empty_plan():
    definition = _definition(norm_test=None)
    plan = plan_completion(_assessment(), [], [], definition, lookup=lambda *a: {})
    assert plan.writes == []


def test_non_needs_review_status_returns_empty_plan_I2():
    definition = _definition()
    metrics_rows = [_metric_row("pr_total", provenance="blank")]
    subtest_rows = [_subtest_row("ZE", rw=2, provenance="blank")]
    plan = plan_completion(
        _assessment(status="validated_ok"),
        metrics_rows,
        subtest_rows,
        definition,
        lookup=lambda *a: {},
    )
    assert plan.writes == []


def test_only_sheet_and_manual_cells_returns_empty_plan_I1():
    definition = _definition()
    metrics_rows = [_metric_row("pr_total", value_num=50, provenance="sheet")]
    subtest_rows = [_subtest_row("ZE", rw=2, pr=90, provenance="manual")]
    plan = plan_completion(
        _assessment(), metrics_rows, subtest_rows, definition, lookup=lambda *a: {}
    )
    assert plan.writes == []


def test_illegible_or_none_provenance_cells_never_touched():
    # save_assessment never assigns 'blank'/'unavailable'/'computed' to a
    # pr_state='illegible' or None cell — it stays 'sheet'. Confirm the
    # planner treats a NULL provenance (legacy row, ADR 0010) the same way:
    # not owned, never touched.
    definition = _definition()
    metrics_rows = [_metric_row("pr_total", value_num=50, provenance=None)]
    subtest_rows = [_subtest_row("ZE", rw=2, pr=90, provenance=None)]
    plan = plan_completion(
        _assessment(), metrics_rows, subtest_rows, definition, lookup=lambda *a: {}
    )
    assert plan.writes == []


# --- subtest PR skip reasons -----------------------------------------------


def test_subtest_no_norm_scale_is_unavailable():
    definition = _definition(
        subtests=[
            SubtestDef(
                key="B", name="Geometrie", rw_max=4, value_type="int", position=1
            )
        ],
        rules_sum=[],
    )
    subtest_rows = [_subtest_row("B", rw=2, provenance="blank")]
    plan = plan_completion(
        _assessment(), [], subtest_rows, definition, lookup=lambda *a: {}
    )
    assert len(plan.writes) == 1
    write = plan.writes[0]
    assert write.provenance == "unavailable"
    assert write.pr is None
    skip = next(s for s in plan.skipped if s.field_ref == "subtest:B")
    assert skip.reason == "no_norm_scale"


def test_subtest_rw_missing_is_blank():
    definition = _definition()
    # Starts 'computed' (a stale cached PR) so the transition to 'blank' is
    # an actual change — "emit only changes" (step 6) would otherwise
    # suppress an already-blank -> blank no-op write.
    subtest_rows = [_subtest_row("ZE", rw=None, pr=5, provenance="computed")]
    plan = plan_completion(
        _assessment(), [], subtest_rows, definition, lookup=lambda *a: {}
    )
    assert plan.writes[0].provenance == "blank"
    assert plan.skipped[0].reason == "rw_missing"


def test_subtest_no_norm_variant_is_blank():
    definition = _definition()
    subtest_rows = [_subtest_row("ZE", rw=2, pr=5, provenance="computed")]
    plan = plan_completion(
        _assessment(norm_variant=None),
        [],
        subtest_rows,
        definition,
        lookup=lambda *a: {},
    )
    assert plan.writes[0].provenance == "blank"
    assert plan.skipped[0].reason == "no_norm_variant"


def test_subtest_invalid_norm_variant_is_blank():
    definition = _definition()
    subtest_rows = [_subtest_row("ZE", rw=2, pr=5, provenance="computed")]
    plan = plan_completion(
        _assessment(norm_variant="not-a-real-variant"),
        [],
        subtest_rows,
        definition,
        lookup=lambda *a: {},
    )
    assert plan.writes[0].provenance == "blank"
    assert plan.skipped[0].reason == "invalid_norm_variant"


def test_subtest_rw_not_in_table_is_blank():
    definition = _definition()
    subtest_rows = [_subtest_row("ZE", rw=99, pr=5, provenance="computed")]
    plan = plan_completion(
        _assessment(),
        [],
        subtest_rows,
        definition,
        lookup=_fake_lookup({}),  # no row -> RuntimeError for any key
    )
    assert plan.writes[0].provenance == "blank"
    assert plan.skipped[0].reason == "rw_not_in_table"


def test_subtest_cell_misprint_is_blank_I8():
    definition = _definition()
    subtest_rows = [_subtest_row("ZE", rw=0, pr=5, provenance="computed")]
    table = {
        ("demat2plus", "subtest_ZE", "k3_gesamt", 0.0): {
            "status": "druckfehler_vermutet",
            "pr": 7,
        }
    }
    plan = plan_completion(
        _assessment(), [], subtest_rows, definition, lookup=_fake_lookup(table)
    )
    assert plan.writes[0].provenance == "blank"
    assert plan.writes[0].pr is None
    assert plan.skipped[0].reason == "cell_misprint"


def test_subtest_not_published_pr_is_unavailable():
    # DEMAT 1+-style 'leer_im_original': pr is None but the row is otherwise ok.
    definition = _definition()
    subtest_rows = [_subtest_row("ZE", rw=0, provenance="blank")]
    table = {
        ("demat2plus", "subtest_ZE", "k3_gesamt", 0.0): {
            "status": "leer_im_original",
            "pr": None,
        }
    }
    plan = plan_completion(
        _assessment(), [], subtest_rows, definition, lookup=_fake_lookup(table)
    )
    assert plan.writes[0].provenance == "unavailable"
    assert plan.skipped[0].reason == "not_published"


def test_subtest_computed_ok():
    definition = _definition()
    subtest_rows = [_subtest_row("ZE", rw=2, provenance="blank")]
    table = {("demat2plus", "subtest_ZE", "k3_gesamt", 2.0): {"status": "ok", "pr": 20}}
    plan = plan_completion(
        _assessment(), [], subtest_rows, definition, lookup=_fake_lookup(table)
    )
    write = plan.writes[0]
    assert write.provenance == "computed"
    assert write.pr == 20
    assert plan.norm_group == "k3_gesamt"
    assert "PR 20" in write.detail


def test_subtest_unsicher_note_appended():
    definition = _definition()
    subtest_rows = [_subtest_row("ZE", rw=2, provenance="blank")]
    table = {
        ("demat2plus", "subtest_ZE", "k3_gesamt", 2.0): {"status": "unsicher", "pr": 20}
    }
    plan = plan_completion(
        _assessment(), [], subtest_rows, definition, lookup=_fake_lookup(table)
    )
    assert "blass gedruckt" in plan.writes[0].detail


def test_subtest_unavailable_to_computed_and_back_when_rw_changes():
    definition = _definition()
    table = {
        ("demat2plus", "subtest_ZE", "k3_gesamt", 0.0): {
            "status": "leer_im_original",
            "pr": None,
        },
        ("demat2plus", "subtest_ZE", "k3_gesamt", 2.0): {"status": "ok", "pr": 20},
    }
    lookup = _fake_lookup(table)

    subtest_rows = [_subtest_row("ZE", rw=0, provenance="blank")]
    plan = plan_completion(_assessment(), [], subtest_rows, definition, lookup=lookup)
    assert plan.writes[0].provenance == "unavailable"

    # Now the RW got corrected/re-read as 2, and the cell is still
    # completion-owned ('unavailable' is in COMPLETION_OWNED):
    subtest_rows2 = [_subtest_row("ZE", rw=2, provenance="unavailable")]
    plan2 = plan_completion(_assessment(), [], subtest_rows2, definition, lookup=lookup)
    assert plan2.writes[0].provenance == "computed"
    assert plan2.writes[0].pr == 20

    # And back to a 'leer_im_original' RW:
    subtest_rows3 = [_subtest_row("ZE", rw=0, provenance="computed", pr=20)]
    plan3 = plan_completion(_assessment(), [], subtest_rows3, definition, lookup=lookup)
    assert plan3.writes[0].provenance == "unavailable"


# --- I4: KB unavailable -----------------------------------------------------


def test_kb_unavailable_discards_writes_and_does_not_clear_I4():
    definition = _definition()
    subtest_rows = [
        _subtest_row("ZE", rw=2, provenance="blank"),
        _subtest_row("AD", rw=4, provenance="blank", position=2),
    ]

    def lookup(*args: Any) -> dict[str, Any]:
        raise KnowledgeBaseUnavailableError("Knowledge base not found at /nope.")

    plan = plan_completion(_assessment(), [], subtest_rows, definition, lookup=lookup)
    assert plan.writes == []
    assert plan.skipped == []
    assert plan.kb_unavailable is not None


# --- I5: missing/invalid variant --------------------------------------------


def test_missing_variant_blanks_lookup_targets_but_not_rw_sum_I5():
    definition = _definition()
    metrics_rows = [_metric_row("rw_total", provenance="blank")]
    subtest_rows = [
        _subtest_row("ZE", rw=2, pr=5, provenance="computed"),
        _subtest_row("AD", rw=4, provenance="blank", position=2),
    ]
    plan = plan_completion(
        _assessment(norm_variant=None),
        metrics_rows,
        subtest_rows,
        definition,
        lookup=lambda *a: (_ for _ in ()).throw(AssertionError("must not call KB")),
    )
    rw_total_write = next(w for w in plan.writes if w.field_ref == "metric:rw_total")
    assert rw_total_write.provenance == "computed"
    assert rw_total_write.value_num == 6.0
    ze_write = next(w for w in plan.writes if w.field_ref == "subtest:ZE")
    assert ze_write.provenance == "blank"


# --- gesamtwert: ≤1 verbatim, unavailable, druckfehler ----------------------


def test_gesamtwert_computed_ok():
    definition = _definition()
    metrics_rows = [
        _metric_row("rw_total", value_num=10.0, provenance="sheet"),
        _metric_row("pr_total", provenance="blank", position=2),
        _metric_row("t_total", provenance="blank", position=4),
    ]
    table = {
        ("demat2plus", "gesamtwert", "k3_gesamt", 10.0): {
            "status": "ok",
            "pr": "14",
            "t_wert": "41",
        }
    }
    plan = plan_completion(
        _assessment(), metrics_rows, [], definition, lookup=_fake_lookup(table)
    )
    by_ref = {w.field_ref: w for w in plan.writes}
    assert by_ref["metric:pr_total"].provenance == "computed"
    assert by_ref["metric:pr_total"].value_num == 14.0
    assert by_ref["metric:pr_total"].value_raw == "14"
    assert by_ref["metric:t_total"].value_num == 41.0


def test_gesamtwert_le1_stored_verbatim_with_null_value_num():
    definition = _definition()
    metrics_rows = [
        _metric_row("rw_total", value_num=2.0, provenance="sheet"),
        _metric_row("pr_total", provenance="blank", position=2),
    ]
    table = {
        ("demat1plus", "gesamtwert", "k2_gesamt", 2.0): {"status": "ok", "pr": "≤1"}
    }
    definition = _definition(test_key="demat1plus", norm_test="demat1plus")
    plan = plan_completion(
        _assessment(norm_variant="k2"),
        metrics_rows,
        [],
        definition,
        lookup=_fake_lookup(table),
    )
    write = next(w for w in plan.writes if w.field_ref == "metric:pr_total")
    assert write.provenance == "computed"
    assert write.value_raw == "≤1"
    assert write.value_text == "≤1"
    assert write.value_num is None


def test_gesamtwert_not_published_field_is_unavailable():
    definition = _definition()
    metrics_rows = [
        _metric_row("rw_total", value_num=10.0, provenance="sheet"),
        _metric_row("pr_band", provenance="blank", position=3),
    ]
    table = {
        ("demat2plus", "gesamtwert", "k3_gesamt", 10.0): {
            "status": "ok",
            "pr": "14",
            "pr_band": None,
        }
    }
    plan = plan_completion(
        _assessment(), metrics_rows, [], definition, lookup=_fake_lookup(table)
    )
    write = next(w for w in plan.writes if w.field_ref == "metric:pr_band")
    assert write.provenance == "unavailable"
    reason = next(s for s in plan.skipped if s.field_ref == "metric:pr_band")
    assert reason.reason == "not_published"


def test_gesamtwert_druckfehler_blanks_all_targets_I8():
    definition = _definition()
    metrics_rows = [
        _metric_row("rw_total", value_num=22.0, provenance="sheet"),
        _metric_row(
            "pr_total", value_num=99, value_raw="99", provenance="computed", position=2
        ),
        _metric_row(
            "t_total", value_num=99, value_raw="99", provenance="computed", position=4
        ),
    ]
    table = {
        ("demat2plus", "gesamtwert", "k3_gesamt", 22.0): {
            "status": "druckfehler_vermutet",
            "pr": "50",
            "t_wert": "50",
        }
    }
    plan = plan_completion(
        _assessment(), metrics_rows, [], definition, lookup=_fake_lookup(table)
    )
    for ref in ("metric:pr_total", "metric:t_total"):
        write = next(w for w in plan.writes if w.field_ref == ref)
        assert write.provenance == "blank"
        assert write.value_num is None
        assert write.value_raw is None


# --- RW(ges) sum-fill --------------------------------------------------------


def test_rw_sum_fill_happy_path():
    definition = _definition()
    metrics_rows = [_metric_row("rw_total", provenance="blank")]
    subtest_rows = [
        _subtest_row("ZE", rw=2, provenance="sheet"),
        _subtest_row("AD", rw=4, provenance="sheet", position=2),
    ]
    plan = plan_completion(
        _assessment(), metrics_rows, subtest_rows, definition, lookup=lambda *a: {}
    )
    write = next(w for w in plan.writes if w.field_ref == "metric:rw_total")
    assert write.provenance == "computed"
    assert write.value_num == 6.0
    assert write.value_raw == "6"


def test_rw_sum_fill_counts_a_zero_subtest_rw():
    """RW 0 is a real value, not "missing" — it must not block the sum."""
    definition = _definition()
    metrics_rows = [_metric_row("rw_total", provenance="blank")]
    subtest_rows = [
        _subtest_row("ZE", rw=0, provenance="sheet"),
        _subtest_row("AD", rw=3, provenance="sheet", position=2),
    ]
    plan = plan_completion(
        _assessment(), metrics_rows, subtest_rows, definition, lookup=lambda *a: {}
    )
    write = next(w for w in plan.writes if w.field_ref == "metric:rw_total")
    assert write.provenance == "computed"
    assert write.value_num == 3.0
    assert write.value_raw == "3"


def test_rw_sum_fill_with_db_loaded_definition(tmp_path, monkeypatch):
    """Regression: `store.get_test_definition` (the path `run_completion`
    uses) dropped `rules.sum` on the DB round-trip, so RW(ges) was never
    sum-filled in the app. Real DEMAT 3+ definition, one subtest at RW 0;
    its grouped rules (ADR 0015) must survive the round-trip too.
    """
    from demat import config, store

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "demat.db")
    monkeypatch.setattr(config, "PAGES_DIR", tmp_path / "pages")
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(config, "UPLOADS_DIR", tmp_path / "uploads")
    monkeypatch.setattr(config, "SALT_PATH", tmp_path / ".salt")
    store.init_db()

    definition = store.get_test_definition("DEMAT 3+")
    assert definition is not None
    assert [(r.subtest, r.metric, r.over) for r in definition.rules_sum] == [
        ("ARI", None, ["ZS", "AD", "SU", "MU"]),
        ("SAR", None, ["SR1", "SR2", "LU"]),
        ("GEO", None, ["SZ", "FL", "LS"]),
        (None, "rw_total", ["ARI", "SAR", "GEO"]),
    ]

    metrics_rows = [
        _metric_row("rw_total", position=1, provenance="blank"),
        _metric_row("pr_total", position=2, provenance="blank"),
    ]
    subtest_rows = [
        _subtest_row("ARI", rw=3, provenance="sheet", position=1),
        _subtest_row("SAR", rw=0, provenance="sheet", position=2),
        _subtest_row("GEO", rw=2, provenance="sheet", position=3),
    ]
    lookup = _fake_lookup(
        {
            ("demat3plus", "subtest_arithmetik", "anfang_k4", 3): {"pr": "11"},
            ("demat3plus", "subtest_sachrechnen", "anfang_k4", 0): {"pr": "22"},
            ("demat3plus", "subtest_geometrie", "anfang_k4", 2): {"pr": "33"},
            ("demat3plus", "gesamtwert", "anfang_k4", 5): {"pr": "44"},
        }
    )
    plan = plan_completion(
        _assessment(norm_variant="anfang_k4"),
        metrics_rows,
        subtest_rows,
        definition,
        lookup=lookup,
    )
    by_ref = {w.field_ref: w for w in plan.writes}
    assert by_ref["metric:rw_total"].provenance == "computed"
    assert by_ref["metric:rw_total"].value_num == 5.0
    assert by_ref["metric:rw_total"].value_raw == "5"
    assert by_ref["metric:pr_total"].provenance == "computed"
    assert by_ref["metric:pr_total"].value_num == 44.0
    assert not [s for s in plan.skipped if s.field_ref.startswith("metric:")]


def test_rw_sum_fill_half_point_formats_with_comma():
    definition = _definition()
    metrics_rows = [_metric_row("rw_total", provenance="blank")]
    subtest_rows = [
        _subtest_row("ZE", rw=2, provenance="sheet"),
        _subtest_row("AD", rw=1.5, provenance="sheet", position=2),
    ]
    plan = plan_completion(
        _assessment(), metrics_rows, subtest_rows, definition, lookup=lambda *a: {}
    )
    write = next(w for w in plan.writes if w.field_ref == "metric:rw_total")
    assert write.value_raw == "3,5"


def test_rw_sum_fill_incomplete_subtests_is_blank():
    definition = _definition()
    metrics_rows = [
        _metric_row("rw_total", value_num=2, value_raw="2", provenance="computed")
    ]
    subtest_rows = [_subtest_row("ZE", rw=2, provenance="sheet")]  # AD missing
    plan = plan_completion(
        _assessment(), metrics_rows, subtest_rows, definition, lookup=lambda *a: {}
    )
    write = next(w for w in plan.writes if w.field_ref == "metric:rw_total")
    assert write.provenance == "blank"
    reason = next(s for s in plan.skipped if s.field_ref == "metric:rw_total")
    assert reason.reason == "sum_incomplete"


def test_rw_sum_fill_duplicate_subtest_is_blank():
    definition = _definition()
    metrics_rows = [
        _metric_row("rw_total", value_num=2, value_raw="2", provenance="computed")
    ]
    subtest_rows = [
        _subtest_row("ZE", rw=2, provenance="sheet"),
        _subtest_row("ZE", rw=1, provenance="sheet", position=2),
        _subtest_row("AD", rw=4, provenance="sheet", position=3),
    ]
    plan = plan_completion(
        _assessment(), metrics_rows, subtest_rows, definition, lookup=lambda *a: {}
    )
    write = next(w for w in plan.writes if w.field_ref == "metric:rw_total")
    assert write.provenance == "blank"


def test_rw_sum_fill_unknown_subtest_is_blank():
    definition = _definition()
    metrics_rows = [
        _metric_row("rw_total", value_num=2, value_raw="2", provenance="computed")
    ]
    subtest_rows = [
        _subtest_row("ZE", rw=2, provenance="sheet"),
        _subtest_row("AD", rw=4, provenance="sheet", position=2),
        _subtest_row("XYZ", rw=1, provenance="sheet", position=3),
    ]
    plan = plan_completion(
        _assessment(), metrics_rows, subtest_rows, definition, lookup=lambda *a: {}
    )
    write = next(w for w in plan.writes if w.field_ref == "metric:rw_total")
    assert write.provenance == "blank"


# --- idempotence / re-derivation --------------------------------------------


def test_idempotent_rerun_produces_no_writes():
    definition = _definition()
    subtest_rows = [_subtest_row("ZE", rw=2, provenance="blank")]
    table = {("demat2plus", "subtest_ZE", "k3_gesamt", 2.0): {"status": "ok", "pr": 20}}
    lookup = _fake_lookup(table)
    plan1 = plan_completion(_assessment(), [], subtest_rows, definition, lookup=lookup)
    assert len(plan1.writes) == 1

    # Apply the write manually to simulate persistence, then re-run.
    subtest_rows2 = [_subtest_row("ZE", rw=2, pr=20, provenance="computed")]
    plan2 = plan_completion(_assessment(), [], subtest_rows2, definition, lookup=lookup)
    assert plan2.writes == []


def test_rw_change_rederives_affected_cell():
    definition = _definition()
    table = {
        ("demat2plus", "subtest_ZE", "k3_gesamt", 2.0): {"status": "ok", "pr": 20},
        ("demat2plus", "subtest_ZE", "k3_gesamt", 1.0): {"status": "ok", "pr": 9},
    }
    lookup = _fake_lookup(table)
    subtest_rows = [_subtest_row("ZE", rw=2, pr=20, provenance="computed")]
    plan = plan_completion(_assessment(), [], subtest_rows, definition, lookup=lookup)
    assert plan.writes == []  # nothing changed yet

    subtest_rows2 = [_subtest_row("ZE", rw=1, pr=20, provenance="computed")]
    plan2 = plan_completion(_assessment(), [], subtest_rows2, definition, lookup=lookup)
    assert plan2.writes[0].pr == 9


# --- I3: no LLM call anywhere in this module --------------------------------

_FORBIDDEN_MODULES = {
    "demat.agent_runner",
    "demat.extraction",
    "demat.reports.prose",
    "claude_agent_sdk",
    "anthropic",
}


def test_norm_completion_module_imports_no_llm_runner():
    import ast
    from pathlib import Path

    import demat.norm_completion as nc

    source = Path(nc.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    forbidden_hits = imported & _FORBIDDEN_MODULES
    assert not forbidden_hits, (
        f"norm_completion.py imports an LLM/runner module: {forbidden_hits}"
    )


def test_default_lookup_is_norms_norm_with_allow_flagged():
    import demat.norm_completion as nc
    from demat.reports import norms

    assert nc._DEFAULT_LOOKUP.func is norms.norm
    assert nc._DEFAULT_LOOKUP.keywords == {"allow_flagged": True}
