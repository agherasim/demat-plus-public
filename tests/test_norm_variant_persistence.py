"""ADR 0010: `assessments.norm_variant` persistence — the save/get
round-trip (the guard against a missing `_AGNOSTIC_COLUMNS` entry, which
would otherwise leave the whole feature a silent no-op), validation against
the row's `norm_test`, and that nothing recomputes a stored value.
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


def _save(sheet, *, norm_variant: str | None = None) -> int:
    store.upsert_source_file("file-sha-1", "test.pdf", 1)
    definition = store.get_test_definition(sheet.test_version)
    triage = TriageResult(
        is_demat_sheet=True,
        test_version=sheet.test_version,
        test_form=sheet.test_form,
        confidence="high",
    )
    validation = validate(sheet, triage, definition)
    computed_person_id, normalization_key = identity.person_id_for(
        sheet.child_name, TEST_SALT
    )
    result = store.save_assessment(
        file_sha256="file-sha-1",
        page_index=0,
        page_key="page-key-1",
        page_image_path="/tmp/page.png",
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
        norm_variant=norm_variant,
    )
    return result["assessment_id"]


def test_save_assessment_norm_variant_round_trips(isolated_db):
    """The guard against a missing `_AGNOSTIC_COLUMNS` entry — without it
    this test stays green while every row silently loses the value."""
    assessment_id = _save(_sheet(), norm_variant="k3")

    row = store.get_assessment(assessment_id)
    assert row is not None
    assert row["norm_variant"] == "k3"

    listed = store.list_assessments()
    assert listed[0]["norm_variant"] == "k3"


def test_save_assessment_rejects_variant_not_valid_for_norm_test(isolated_db):
    with pytest.raises(ValueError):
        _save(_sheet(), norm_variant="gymnasium")  # DEMAT 2+ has no 'gymnasium'


def test_update_assessment_fields_persists_norm_variant_and_stamps_edited_at(
    isolated_db,
):
    assessment_id = _save(_sheet())
    store.update_assessment_fields(assessment_id, {"norm_variant": "k2"})

    row = store.get_assessment(assessment_id)
    assert row is not None
    assert row["norm_variant"] == "k2"
    assert row["edited_at"] is not None


def test_update_assessment_fields_rejects_unknown_norm_variant(isolated_db):
    assessment_id = _save(_sheet())
    with pytest.raises(ValueError):
        store.update_assessment_fields(assessment_id, {"norm_variant": "nonsense"})


def test_null_norm_variant_stays_null_after_unrelated_edit(isolated_db):
    assessment_id = _save(_sheet())
    store.update_assessment_fields(assessment_id, {"test_date_iso": "2026-09-20"})

    row = store.get_assessment(assessment_id)
    assert row is not None
    assert row["norm_variant"] is None
