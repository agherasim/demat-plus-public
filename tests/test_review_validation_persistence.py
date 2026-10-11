"""Tests for persisting review-validation results (`store.save_review_validation`,
`demat.review_validation.findings_to_json`/`findings_from_json`) and for
`init_db()`'s additive column backfill on a database created before these
columns existed.
"""

from __future__ import annotations

import sqlite3

import pytest

from demat import config, identity, store
from demat.review_validation import (
    ReviewFinding,
    findings_from_json,
    findings_to_json,
)
from demat.schema import ExtractedSheet, TriageResult
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


def _sheet() -> ExtractedSheet:
    return ExtractedSheet(
        is_demat_sheet=True,
        test_version="DEMAT 2+",
        test_form="A",
        child_name="Max Mustermann",
        child_name_confidence="high",
        klasse="3c",
        gender="junge",
        test_date_raw="16.09.26",
        test_date_iso="2026-09-16",
        test_date_confidence="high",
        subtests=[],
        overall=[],
        assessment_note=None,
        illegible_notes=None,
        overall_confidence="high",
    )


def _save(isolated_db) -> int:
    sheet = _sheet()
    store.upsert_source_file("file-sha-1", "test.pdf", 1)
    definition = store.get_test_definition(sheet.test_version)
    triage = TriageResult(
        is_demat_sheet=True, test_version="DEMAT 2+", test_form="A", confidence="high"
    )
    validation = validate(sheet, triage, definition)
    computed_person_id, normalization_key = identity.person_id_for(
        sheet.child_name, TEST_SALT
    )
    result = store.save_assessment(
        file_sha256="file-sha-1",
        page_index=0,
        page_key="page-key-1",
        page_image_path=str(isolated_db / "page.png"),
        extracted=sheet,
        triage=triage,
        extraction_skill="demat-2plus-extraction",
        validation=validation,
        definition=definition,
        model_id="test-model",
        runner="direct",
        raw_response={"is_demat_sheet": True, "child_name": sheet.child_name},
        computed_person_id=computed_person_id,
        normalization_key=normalization_key,
        raw_name=sheet.child_name,
    )
    return result["assessment_id"]


def test_findings_json_roundtrip():
    findings = [
        ReviewFinding("values_consistent", "error", "sums don't match"),
        ReviewFinding("norm_mapping", "warning", "kb unavailable"),
    ]
    restored = findings_from_json(findings_to_json(findings))
    assert restored == findings


def test_findings_from_json_handles_none_and_empty():
    assert findings_from_json(None) == []
    assert findings_from_json("") == []


def test_save_review_validation_is_read_back_by_get_assessment(isolated_db):
    assessment_id = _save(isolated_db)
    findings = [ReviewFinding("values_consistent", "error", "bad sum")]

    store.save_review_validation(
        assessment_id, findings_to_json(findings), edited_at_snapshot=None
    )

    row = store.get_assessment(assessment_id)
    assert row is not None
    assert findings_from_json(row["review_validation_json"]) == findings
    assert row["review_validation_at"] is not None
    assert row["review_validation_edited_at"] is None


def test_new_assessment_has_no_validation_until_run(isolated_db):
    assessment_id = _save(isolated_db)
    row = store.get_assessment(assessment_id)
    assert row is not None
    assert row["review_validation_json"] is None
    assert row["review_validation_at"] is None


def test_init_db_backfills_columns_on_pre_existing_database(isolated_db):
    """Simulates a database created before `review_validation_*` existed:
    drop the columns, then re-run `init_db()` and confirm they come back
    without needing a destructive reset.
    """
    assessment_id = _save(isolated_db)

    conn = sqlite3.connect(config.DB_PATH)
    conn.execute("ALTER TABLE assessments DROP COLUMN review_validation_json")
    conn.execute("ALTER TABLE assessments DROP COLUMN review_validation_at")
    conn.execute("ALTER TABLE assessments DROP COLUMN review_validation_edited_at")
    conn.execute("ALTER TABLE assessments DROP COLUMN norm_variant")
    conn.commit()
    conn.close()

    store.init_db()

    row = store.get_assessment(assessment_id)
    assert row is not None
    assert "review_validation_json" in row
    assert "norm_variant" in row
