"""Integration tests for the `demat.store` side of ADR 0013: the
`save_assessment` provenance mapping, the additive migration,
`apply_norm_completion`, and the updaters' `manual` transitions.
"""

from __future__ import annotations

import sqlite3

import pytest

from demat import config, identity, norm_completion, store
from demat.schema import ExtractedSheet, MetricValue, SubtestScore, TriageResult
from demat.validation import validate

TEST_SALT = "test-salt"


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "demat.db")
    monkeypatch.setattr(config, "PAGES_DIR", tmp_path / "pages")
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(config, "UPLOADS_DIR", tmp_path / "uploads")
    monkeypatch.setattr(config, "SALT_PATH", tmp_path / ".salt")
    store.init_db()
    return tmp_path


def _sheet(**overrides) -> ExtractedSheet:
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


def _save(
    isolated_db,
    sheet,
    *,
    page_key="page-key-1",
    file_sha256="file-sha-1",
    norm_variant=None,
):
    store.upsert_source_file(file_sha256, "test.pdf", 1)
    definition = store.get_test_definition(sheet.test_version)
    triage = _triage()
    validation = validate(sheet, triage, definition)
    computed_person_id = None
    normalization_key = None
    if sheet.child_name:
        computed_person_id, normalization_key = identity.person_id_for(
            sheet.child_name, TEST_SALT
        )
    return store.save_assessment(
        file_sha256=file_sha256,
        page_index=0,
        page_key=page_key,
        page_image_path=str(isolated_db / "page.png"),
        extracted=sheet,
        triage=triage,
        extraction_skill="demat-2plus-extraction",
        validation=validation,
        definition=definition,
        model_id="test-model",
        runner="direct",
        raw_response={"is_demat_sheet": True},
        computed_person_id=computed_person_id,
        normalization_key=normalization_key,
        raw_name=sheet.child_name,
        norm_variant=norm_variant,
    )


# --- save_assessment provenance mapping -------------------------------------


def test_subtest_provenance_blank_when_pr_none_and_state_blank(isolated_db):
    sheet = _sheet(
        subtests=[
            SubtestScore(key="ZE", rw="2", pr=None, pr_state="blank", confidence="high")
        ]
    )
    result = _save(isolated_db, sheet)
    rows = store.get_subtest_scores(result["assessment_id"])
    assert rows[0]["pr_provenance"] == "blank"


def test_subtest_provenance_sheet_when_pr_written(isolated_db):
    sheet = _sheet(
        subtests=[
            SubtestScore(key="ZE", rw="2", pr=20, pr_state="written", confidence="high")
        ]
    )
    result = _save(isolated_db, sheet)
    rows = store.get_subtest_scores(result["assessment_id"])
    assert rows[0]["pr_provenance"] == "sheet"


def test_subtest_provenance_sheet_when_illegible(isolated_db):
    # A value always wins over a state, and 'illegible' never becomes
    # 'blank' — it stays 'sheet' so completion never touches it (I1).
    sheet = _sheet(
        subtests=[
            SubtestScore(
                key="ZE", rw="2", pr=None, pr_state="illegible", confidence="low"
            )
        ]
    )
    result = _save(isolated_db, sheet)
    rows = store.get_subtest_scores(result["assessment_id"])
    assert rows[0]["pr_provenance"] == "sheet"


def test_metric_provenance_blank_when_value_none_and_state_blank(isolated_db):
    sheet = _sheet(
        overall=[
            MetricValue(
                key="pr_total", value=None, value_state="blank", confidence="high"
            )
        ]
    )
    result = _save(isolated_db, sheet)
    rows = store.get_assessment_metrics(result["assessment_id"])
    assert rows[0]["value_provenance"] == "blank"


def test_metric_provenance_sheet_when_value_written(isolated_db):
    sheet = _sheet(
        overall=[
            MetricValue(
                key="pr_total", value="55", value_state="written", confidence="high"
            )
        ]
    )
    result = _save(isolated_db, sheet)
    rows = store.get_assessment_metrics(result["assessment_id"])
    assert rows[0]["value_provenance"] == "sheet"


def test_metric_provenance_sheet_when_key_not_in_definition(isolated_db):
    sheet = _sheet(
        overall=[
            MetricValue(
                key="not_a_real_metric",
                value=None,
                value_state="blank",
                confidence="high",
            )
        ]
    )
    result = _save(isolated_db, sheet)
    rows = store.get_assessment_metrics(result["assessment_id"])
    assert rows[0]["value_provenance"] == "sheet"


# --- additive migration ------------------------------------------------------


def test_additive_migration_adds_provenance_columns_to_old_schema_db(tmp_path):
    # Simulate a pre-ADR-0013 DB: create the tables without the new columns,
    # then run init_db() again and confirm they're backfilled additively,
    # with no data loss and no destructive rebuild.
    db_path = tmp_path / "demat.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE subtest_scores (assessment_id INTEGER, position INTEGER, "
        "subtest_key TEXT, printed_label TEXT, name TEXT, value_raw TEXT, rw REAL, "
        "rw_max REAL, pr INTEGER, confidence TEXT, PRIMARY KEY (assessment_id, position))"
    )
    conn.execute(
        "CREATE TABLE assessment_metrics (assessment_id INTEGER, position INTEGER, "
        "metric_key TEXT, value_raw TEXT, value_num REAL, value_text TEXT, "
        "confidence TEXT, PRIMARY KEY (assessment_id, position))"
    )
    conn.execute(
        "INSERT INTO subtest_scores VALUES (1, 1, 'ZE', 'ZE', 'x', '2', 2.0, 2.0, 20, 'high')"
    )
    conn.commit()
    conn.close()

    monkeypatch_data_dir = tmp_path
    import demat.config as config

    old_data_dir, old_db_path = config.DATA_DIR, config.DB_PATH
    try:
        config.DATA_DIR = monkeypatch_data_dir
        config.DB_PATH = db_path
        store.init_db()
        with store.get_connection() as conn2:
            cols = {
                r["name"] for r in conn2.execute("PRAGMA table_info(subtest_scores)")
            }
            assert "pr_provenance" in cols
            row = conn2.execute(
                "SELECT pr, pr_provenance FROM subtest_scores WHERE assessment_id=1"
            ).fetchone()
            assert row["pr"] == 20
            assert row["pr_provenance"] is None  # untouched: reads as 'sheet'
    finally:
        config.DATA_DIR, config.DB_PATH = old_data_dir, old_db_path


# --- apply_norm_completion ---------------------------------------------------


def test_apply_norm_completion_adds_and_removes_flag(isolated_db):
    sheet = _sheet(
        subtests=[
            SubtestScore(key="ZE", rw="2", pr=None, pr_state="blank", confidence="high")
        ]
    )
    result = _save(isolated_db, sheet, norm_variant="k3")
    assessment_id = result["assessment_id"]

    def lookup(norm_test, scale, group, raw):
        return {"status": "ok", "pr": 20}

    plan1 = norm_completion.run_completion(assessment_id, lookup=lookup)
    assert plan1.writes
    assessment = store.get_assessment(assessment_id)
    assert assessment is not None
    assert "norm_values_computed" in assessment["flags"]

    # A reviewer now enters the PR by hand (-> 'manual'), so no computed
    # cell remains -> the flag comes back off on the next completion run.
    store.update_subtest_score(assessment_id, 1, rw=2.0, pr=99)
    plan2 = norm_completion.run_completion(assessment_id, lookup=lookup)
    assessment2 = store.get_assessment(assessment_id)
    assert assessment2 is not None
    assert "norm_values_computed" not in assessment2["flags"]
    assert plan2.writes == []  # the cell is 'manual' now, not owned


def test_apply_norm_completion_appends_note_only_on_write(isolated_db):
    sheet = _sheet(
        subtests=[
            SubtestScore(key="ZE", rw="2", pr=None, pr_state="blank", confidence="high")
        ]
    )
    result = _save(isolated_db, sheet, norm_variant="k3")
    assessment_id = result["assessment_id"]

    def lookup(norm_test, scale, group, raw):
        return {"status": "ok", "pr": 20}

    norm_completion.run_completion(assessment_id, lookup=lookup)
    after_first = store.get_assessment(assessment_id)
    assert after_first is not None
    assert "Normwerte nachgeschlagen" in (after_first["review_notes"] or "")

    # Idempotent re-run: no write, so no second note appended.
    norm_completion.run_completion(assessment_id, lookup=lookup)
    after_second = store.get_assessment(assessment_id)
    assert after_second is not None
    assert after_first["review_notes"] == after_second["review_notes"]


def test_apply_norm_completion_never_stamps_edited_at_I6(isolated_db):
    sheet = _sheet(
        subtests=[
            SubtestScore(key="ZE", rw="2", pr=None, pr_state="blank", confidence="high")
        ]
    )
    result = _save(isolated_db, sheet, norm_variant="k3")
    assessment_id = result["assessment_id"]
    before = store.get_assessment(assessment_id)
    assert before is not None
    assert before["edited_at"] is None

    def lookup(norm_test, scale, group, raw):
        return {"status": "ok", "pr": 20}

    norm_completion.run_completion(assessment_id, lookup=lookup)
    after = store.get_assessment(assessment_id)
    assert after is not None
    assert after["edited_at"] is None


def test_apply_norm_completion_aborts_on_terminal_row(isolated_db):
    sheet = _sheet(
        subtests=[
            SubtestScore(key="ZE", rw="2", pr=None, pr_state="blank", confidence="high")
        ]
    )
    result = _save(isolated_db, sheet, norm_variant="k3")
    assessment_id = result["assessment_id"]
    store.finalize_assessment(assessment_id)

    def lookup(norm_test, scale, group, raw):
        return {"status": "ok", "pr": 20}

    plan = norm_completion.run_completion(assessment_id, lookup=lookup)
    assessment = store.get_assessment(assessment_id)
    assert assessment is not None
    assert assessment["status"] != "needs_review"
    # plan.writes may be non-empty (the planner doesn't know about status
    # changes that happened after it read the row), but nothing is stored.
    rows = store.get_subtest_scores(assessment_id)
    assert rows[0]["pr_provenance"] == "blank"
    assert rows[0]["pr"] is None
    del plan


def test_apply_norm_completion_skips_concurrently_edited_cell(isolated_db):
    from demat.norm_completion import CellWrite, CompletionPlan

    sheet = _sheet(
        subtests=[
            SubtestScore(key="ZE", rw="2", pr=None, pr_state="blank", confidence="high")
        ]
    )
    result = _save(isolated_db, sheet)
    assessment_id = result["assessment_id"]
    row = store.get_subtest_scores(assessment_id)[0]

    # Simulate a reviewer editing the cell to 'manual' between planning and
    # applying.
    store.update_subtest_score(assessment_id, row["position"], rw=2.0, pr=42)

    plan = CompletionPlan(
        writes=[
            CellWrite(
                table="subtest",
                position=row["position"],
                field_ref="subtest:ZE",
                provenance="computed",
                pr=20,
            )
        ],
        norm_group="k3_gesamt",
    )
    store.apply_norm_completion(assessment_id, plan)
    after = store.get_subtest_scores(assessment_id)[0]
    assert after["pr"] == 42
    assert after["pr_provenance"] == "manual"


# --- updater provenance transitions -----------------------------------------


def test_update_subtest_score_pr_change_marks_manual(isolated_db):
    sheet = _sheet(
        subtests=[
            SubtestScore(key="ZE", rw="2", pr=None, pr_state="blank", confidence="high")
        ]
    )
    result = _save(isolated_db, sheet)
    assessment_id = result["assessment_id"]
    store.update_subtest_score(assessment_id, 1, rw=2.0, pr=20)
    row = store.get_subtest_scores(assessment_id)[0]
    assert row["pr_provenance"] == "manual"


def test_update_subtest_score_rw_only_edit_leaves_provenance_unchanged(isolated_db):
    sheet = _sheet(
        subtests=[
            SubtestScore(key="ZE", rw="2", pr=20, pr_state="written", confidence="high")
        ]
    )
    result = _save(isolated_db, sheet)
    assessment_id = result["assessment_id"]
    # Simulate a prior completion run having set this to 'computed'.
    with store.get_connection() as conn:
        conn.execute(
            "UPDATE subtest_scores SET pr_provenance = 'computed' "
            "WHERE assessment_id = ? AND position = 1",
            (assessment_id,),
        )
    store.update_subtest_score(assessment_id, 1, rw=3.0, pr=20)
    row = store.get_subtest_scores(assessment_id)[0]
    assert row["pr_provenance"] == "computed"  # unchanged: pr itself unchanged


def test_update_subtest_score_float_nan_roundtrip_not_treated_as_change(isolated_db):
    # st.data_editor round-trips a blank pr cell as NaN, and an INTEGER pr
    # as a float — neither should look like a "pr changed" edit.
    sheet = _sheet(
        subtests=[
            SubtestScore(key="ZE", rw="2", pr=20, pr_state="written", confidence="high")
        ]
    )
    result = _save(isolated_db, sheet)
    assessment_id = result["assessment_id"]
    with store.get_connection() as conn:
        conn.execute(
            "UPDATE subtest_scores SET pr_provenance = 'computed' "
            "WHERE assessment_id = ? AND position = 1",
            (assessment_id,),
        )
    store.update_subtest_score(assessment_id, 1, rw=2.0, pr=20.0)  # float, same value
    row = store.get_subtest_scores(assessment_id)[0]
    assert row["pr_provenance"] == "computed"


def test_update_assessment_metric_value_change_marks_manual(isolated_db):
    sheet = _sheet(
        overall=[
            MetricValue(
                key="pr_total", value=None, value_state="blank", confidence="high"
            )
        ]
    )
    result = _save(isolated_db, sheet)
    assessment_id = result["assessment_id"]
    store.update_assessment_metric(assessment_id, 1, "55")
    row = store.get_assessment_metrics(assessment_id)[0]
    assert row["value_provenance"] == "manual"


def test_update_assessment_metric_unchanged_value_leaves_provenance(isolated_db):
    sheet = _sheet(
        overall=[
            MetricValue(
                key="pr_total", value="55", value_state="written", confidence="high"
            )
        ]
    )
    result = _save(isolated_db, sheet)
    assessment_id = result["assessment_id"]
    with store.get_connection() as conn:
        conn.execute(
            "UPDATE assessment_metrics SET value_provenance = 'computed' "
            "WHERE assessment_id = ? AND position = 1",
            (assessment_id,),
        )
    store.update_assessment_metric(assessment_id, 1, "55")  # re-saved, unchanged
    row = store.get_assessment_metrics(assessment_id)[0]
    assert row["value_provenance"] == "computed"
