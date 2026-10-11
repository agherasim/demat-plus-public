from __future__ import annotations

from demat import config, store
from demat.schema import ExtractedSheet, TriageResult
from demat.validation import validate


def _sheet(**overrides):
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


def _triage():
    return TriageResult(
        is_demat_sheet=True, test_version="DEMAT 2+", test_form="A", confidence="high"
    )


def test_delete_assessments_removes_row_and_owned_files(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "demat.db")
    monkeypatch.setattr(config, "PAGES_DIR", tmp_path / "pages")
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(config, "UPLOADS_DIR", tmp_path / "uploads")
    monkeypatch.setattr(config, "SALT_PATH", tmp_path / ".salt")
    store.init_db()

    config.PAGES_DIR.mkdir(parents=True, exist_ok=True)
    config.RUNS_DIR.mkdir(parents=True, exist_ok=True)

    page_path = config.PAGES_DIR / "abc123.png"
    page_path.write_bytes(b"fake png")
    run_json = config.RUNS_DIR / "abc123.json"
    run_json.write_text("{}")
    run_triage = config.RUNS_DIR / "abc123.triage.json"
    run_triage.write_text("{}")

    store.upsert_source_file("filehash", "test.pdf", 1)
    sheet = _sheet()
    triage = _triage()
    definition = store.get_test_definition(sheet.test_version)
    validation = validate(sheet, triage, definition)

    result = store.save_assessment(
        file_sha256="filehash",
        page_index=0,
        page_key="abc123",
        page_image_path=str(page_path),
        extracted=sheet,
        triage=triage,
        extraction_skill="demat-2plus-extraction",
        validation=validation,
        definition=definition,
        model_id="test-model",
        runner="direct",
        raw_response={"is_demat_sheet": True},
        computed_person_id=None,
        normalization_key=None,
        raw_name=sheet.child_name,
    )
    assessment_id = result["assessment_id"]

    review_dir = config.RUNS_DIR / f"review_validation_{assessment_id}"
    review_dir.mkdir(parents=True, exist_ok=True)
    (review_dir / "note_check.json").write_text("{}")

    assert store.get_assessment(assessment_id) is not None
    assert page_path.exists()
    assert run_json.exists()
    assert run_triage.exists()
    assert review_dir.exists()

    store.delete_assessments([assessment_id])

    assert store.get_assessment(assessment_id) is None
    assert not page_path.exists()
    assert not run_json.exists()
    assert not run_triage.exists()
    assert not review_dir.exists()

    # deleting an already-gone id is a no-op, not an error
    store.delete_assessments([assessment_id])
