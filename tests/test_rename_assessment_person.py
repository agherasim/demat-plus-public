"""`store.rename_assessment_person`'s first-allocation branch (a corrected
name giving a previously-unlinked assessment its first `person_id`) must
run the same collision-checked resolution `save_assessment` does — see
`store._resolve_person_link`, factored out after a real incident where this
path merged two same-named, different-gender children silently because it
skipped the gender-mismatch check entirely.
"""

from __future__ import annotations

import pytest

from demat import config, identity, store
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
    # rename_assessment_person resolves the salt internally (unlike
    # save_assessment, which takes an already-computed id); pin it to the
    # same salt the test uses when precomputing ids by hand.
    monkeypatch.setenv("DEMAT_ID_SALT", TEST_SALT)
    store.init_db()
    return tmp_path


def _sheet(**overrides) -> ExtractedSheet:
    defaults = {
        "is_demat_sheet": True,
        "test_version": "DEMAT 2+",
        "test_form": "A",
        "child_name": None,
        "child_name_confidence": "low",
        "klasse": "6A",
        "gender": "unknown",
        "test_date_raw": "10.09.26",
        "test_date_iso": "2026-09-10",
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


def _save(isolated_db, sheet, *, page_key, file_sha256):
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


def _flags(assessment_id: int) -> list[str]:
    row = store.get_assessment(assessment_id)
    assert row is not None
    return list(row["flags"])


def test_rename_first_allocation_flags_gender_mismatch(isolated_db):
    # A girl, name known upfront: links immediately, gender on record.
    girl = _save(
        isolated_db,
        _sheet(child_name="Sina Müller", gender="maedchen"),
        page_key="page-1",
        file_sha256="file-1",
    )
    assert girl["person_id"] is not None

    # A second sheet, same class, name missing at extraction time (a real
    # `missing_name` case) but a different, definite gender — unlinked
    # until a reviewer types the name in.
    boy = _save(
        isolated_db,
        _sheet(child_name=None, gender="junge"),
        page_key="page-2",
        file_sha256="file-2",
    )
    assert boy["person_id"] is None

    # The reviewer types in the identical name. This must not silently
    # merge into the girl's person_id.
    result = store.rename_assessment_person(boy["assessment_id"], "Sina Müller")

    assert result["person_id"] == girl["person_id"]
    assert "possible_person_merge" in _flags(boy["assessment_id"])


def test_rename_first_allocation_no_flag_when_gender_agrees(isolated_db):
    first = _save(
        isolated_db,
        _sheet(child_name="Leon Schmidt", gender="junge"),
        page_key="page-1",
        file_sha256="file-1",
    )
    second = _save(
        isolated_db,
        _sheet(child_name=None, gender="junge"),
        page_key="page-2",
        file_sha256="file-2",
    )

    result = store.rename_assessment_person(second["assessment_id"], "Leon Schmidt")

    assert result["person_id"] == first["person_id"]
    assert "possible_person_merge" not in _flags(second["assessment_id"])


def test_rename_first_allocation_still_flags_id_collision(isolated_db, monkeypatch):
    first = _save(
        isolated_db,
        _sheet(child_name="Anna Bauer", gender="maedchen"),
        page_key="page-1",
        file_sha256="file-1",
    )
    second = _save(
        isolated_db,
        _sheet(child_name=None, gender="maedchen"),
        page_key="page-2",
        file_sha256="file-2",
    )

    # Force a hash collision: same computed person_id, different key.
    monkeypatch.setattr(
        identity, "person_id_for", lambda name, salt: (first["person_id"], "other key")
    )

    result = store.rename_assessment_person(second["assessment_id"], "Different Name")

    assert result["person_id"] is None
    assert "id_collision" in _flags(second["assessment_id"])
