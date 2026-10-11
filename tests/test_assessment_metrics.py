"""Integration tests for `demat.store.save_assessment` against the new
long-form `assessment_metrics` table (ADR 0005 §2), plus the four-step
subtest-key resolution as it's actually exercised from `store.py`.
"""

from __future__ import annotations

import pytest

from demat import config, identity, store
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


def _save(isolated_db, sheet, *, page_key="page-key-1", file_sha256="file-sha-1"):
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
    )


def test_save_assessment_writes_metric_rows(isolated_db):
    sheet = _sheet(
        overall=[
            MetricValue(key="rw_total", value="13", confidence="high"),
            MetricValue(key="pr_total", value="10", confidence="high"),
        ]
    )
    result = _save(isolated_db, sheet)
    metrics = store.get_assessment_metrics(result["assessment_id"])
    by_key = {m["metric_key"]: m for m in metrics}
    assert by_key["rw_total"]["value_num"] == 13.0
    assert by_key["rw_total"]["value_raw"] == "13"
    assert by_key["pr_total"]["value_num"] == 10.0


def test_duplicate_metric_key_flags_rather_than_raises(isolated_db):
    sheet = _sheet(
        overall=[
            MetricValue(key="rw_total", value="13", confidence="high"),
            MetricValue(key="rw_total", value="14", confidence="medium"),
        ]
    )
    # Must not raise (PK is (assessment_id, position), not metric_key).
    result = _save(isolated_db, sheet)
    assert "duplicate_metric" in result["flags"]
    metrics = store.get_assessment_metrics(result["assessment_id"])
    assert len(metrics) == 2


def test_coercion_failure_flags_and_still_saves(isolated_db):
    sheet = _sheet(
        overall=[MetricValue(key="rw_total", value="not-a-number", confidence="low")]
    )
    result = _save(isolated_db, sheet)
    assert "metric_unparseable" in result["flags"]
    metrics = store.get_assessment_metrics(result["assessment_id"])
    assert metrics[0]["value_num"] is None
    assert metrics[0]["value_raw"] == "not-a-number"


def test_half_point_score_round_trips_as_float(isolated_db):
    sheet = _sheet(
        subtests=[
            SubtestScore(
                key="SA", name="Sachaufgaben", rw="3,5", rw_max="4", confidence="high"
            )
        ]
    )
    result = _save(isolated_db, sheet)
    subtests = store.get_subtest_scores(result["assessment_id"])
    assert subtests[0]["rw"] == 3.5
    assert subtests[0]["value_raw"] == "3,5"


def test_subtest_key_resolution_applied_on_save(isolated_db):
    # The 2+ skill (or a handwritten transcription) emits the printed label
    # "LÄ"; store.save_assessment must resolve it to the canonical key "LA"
    # so cross-test/cross-skill subtest comparison doesn't fracture.
    sheet = _sheet(
        subtests=[
            SubtestScore(
                key="LÄ", name="Längenvergleich", rw="2", rw_max="4", confidence="high"
            )
        ]
    )
    result = _save(isolated_db, sheet)
    subtests = store.get_subtest_scores(result["assessment_id"])
    assert subtests[0]["subtest_key"] == "LA"
    assert subtests[0]["printed_label"] == "LÄ"


def test_duplicate_suspected_flag(isolated_db):
    sheet = _sheet(child_name="Anna Schmidt")
    first = _save(isolated_db, sheet, page_key="page-1", file_sha256="file-1")
    assert first["person_id"] is not None

    # Same name -> same person_id (name-only key, ADR 0006), same test
    # version and date -> duplicate_suspected.
    second = _save(isolated_db, sheet, page_key="page-2", file_sha256="file-2")
    assert "duplicate_suspected" in second["flags"]
    assert second["has_blocking_flags"] is True
