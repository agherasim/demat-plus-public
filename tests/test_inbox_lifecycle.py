"""Tests for the ADR 0007 four-state assessment lifecycle and the Inbox
store functions: T1-T5 transitions, `mark_opened` idempotence,
`list_inbox_assessments` ordering, `finalize_assessment`'s terminal-state
refusal, and edit coercion/provenance guarantees.
"""

from __future__ import annotations

import json
import sqlite3

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
        raw_response={"is_demat_sheet": True, "child_name": sheet.child_name},
        computed_person_id=computed_person_id,
        normalization_key=normalization_key,
        raw_name=sheet.child_name,
    )


def _status(isolated_db, assessment_id: int) -> str:
    conn = sqlite3.connect(config.DB_PATH)
    row = conn.execute(
        "SELECT status FROM assessments WHERE assessment_id = ?", (assessment_id,)
    ).fetchone()
    conn.close()
    return row[0]


def _assessment(assessment_id: int) -> dict:
    """`store.get_assessment` narrowed to non-None for test call sites; the
    row is expected to exist (the assessment was just saved above)."""
    row = store.get_assessment(assessment_id)
    assert row is not None
    return row


def _full_sheet_with_metric() -> ExtractedSheet:
    return _sheet(
        overall=[MetricValue(key="rw_total", value="13", confidence="high")],
        subtests=[
            SubtestScore(
                key="SA", name="Sachaufgaben", rw="3,5", rw_max="4", confidence="high"
            )
        ],
    )


# --- T1: extraction saves a sheet -> needs_review --------------------------


def test_t1_save_assessment_starts_needs_review(isolated_db):
    result = _save(isolated_db, _sheet())
    assert _status(isolated_db, result["assessment_id"]) == "needs_review"


# --- T2: save an edited field -> stays needs_review, stamps edited_at ------


def test_t2_edit_field_stays_needs_review_and_stamps_edited_at(isolated_db):
    result = _save(isolated_db, _sheet())
    assessment_id = result["assessment_id"]

    store.update_assessment_fields(assessment_id, {"klasse": "4a"})

    row = _assessment(assessment_id)
    assert row["status"] == "needs_review"
    assert row["edited_at"] is not None
    assert "manual edit" in (row["review_notes"] or "")
    assert row["klasse"] == "4A"


def test_t2_edit_then_revert_still_counts_as_edited(isolated_db):
    """An edit-then-revert is provenance, not a diff: `edited_at` stays set."""
    result = _save(isolated_db, _sheet(klasse="3c"))
    assessment_id = result["assessment_id"]

    store.update_assessment_fields(assessment_id, {"klasse": "4a"})
    first_edited_at = _assessment(assessment_id)["edited_at"]

    store.update_assessment_fields(assessment_id, {"klasse": "3c"})
    row = _assessment(assessment_id)
    assert row["klasse"] == "3C"
    assert row["edited_at"] == first_edited_at  # unchanged, not re-stamped, not cleared


def test_klasse_is_uppercased_on_save(isolated_db):
    """`save_assessment` always uppercases Klasse (ADR 0007 display rule)."""
    result = _save(isolated_db, _sheet(klasse="3c"))
    row = _assessment(result["assessment_id"])
    assert row["klasse"] == "3C"


def test_klasse_is_uppercased_on_manual_edit(isolated_db):
    """`update_assessment_fields` uppercases Klasse too, independent of what
    the caller passes in — the UI diffs case-insensitively, but the store
    layer doesn't trust that and normalizes on its own.
    """
    result = _save(isolated_db, _sheet())
    assessment_id = result["assessment_id"]

    store.update_assessment_fields(assessment_id, {"klasse": "4b"})

    row = _assessment(assessment_id)
    assert row["klasse"] == "4B"


# --- Name correction: person_id is permanent once allocated ----------------


def test_rename_does_not_change_person_id_once_allocated(isolated_db):
    """A name correction on an already-linked assessment never re-derives
    `person_id` — only the stored spelling changes."""
    result = _save(isolated_db, _sheet())
    assessment_id = result["assessment_id"]
    original_person_id = result["person_id"]
    assert original_person_id is not None

    outcome = store.rename_assessment_person(
        assessment_id, "Someone Entirely Different"
    )

    row = _assessment(assessment_id)
    assert row["person_id"] == original_person_id
    assert outcome["person_id"] == original_person_id
    assert row["edited_at"] is not None
    assert "name corrected" in (row["review_notes"] or "")


def test_rename_allocates_person_id_for_unlinked_assessment(isolated_db):
    """An unlinked assessment (no `person_id` yet) treats a name edit as a
    first allocation, same collision-checked resolution as save_assessment."""
    result = _save(isolated_db, _sheet(child_name=None))
    assessment_id = result["assessment_id"]
    assert result["person_id"] is None

    outcome = store.rename_assessment_person(assessment_id, "Neu Kind")

    row = _assessment(assessment_id)
    assert row["person_id"] is not None
    assert outcome["person_id"] == row["person_id"]
    assert row["person_name"] == "Neu Kind"


def test_rename_links_two_unlinked_assessments_to_the_same_person(isolated_db):
    """Two unlinked assessments corrected to the same name resolve to the
    same `person_id` (the existing-key branch of first allocation)."""
    first = _save(isolated_db, _sheet(child_name=None), page_key="page-key-a")
    second = _save(
        isolated_db,
        _sheet(child_name=None),
        page_key="page-key-b",
        file_sha256="file-sha-2",
    )

    store.rename_assessment_person(first["assessment_id"], "Same Kind")
    store.rename_assessment_person(second["assessment_id"], "Same Kind")

    row1 = _assessment(first["assessment_id"])
    row2 = _assessment(second["assessment_id"])
    assert row1["person_id"] == row2["person_id"]


def test_rename_rejects_blank_name(isolated_db):
    result = _save(isolated_db, _sheet())
    with pytest.raises(ValueError):
        store.rename_assessment_person(result["assessment_id"], "   ")


def test_rename_self_heals_orphaned_person_id(isolated_db):
    """A `person_id` with no matching `persons` row (data that predates the
    FK guarantee, or was inserted outside the app) must not crash the
    rename — it backfills `persons` under the existing, unchanged
    person_id rather than raising IntegrityError.
    """
    result = _save(isolated_db, _sheet())
    assessment_id = result["assessment_id"]
    orphan_id = "m_schneider"
    # Foreign keys off for this setup only, to reproduce data that was
    # written outside the app's own FK-enforcing connections (e.g. a raw
    # sqlite3 session) rather than something the app itself can create.
    conn = sqlite3.connect(config.DB_PATH)
    conn.execute(
        "UPDATE assessments SET person_id = ? WHERE assessment_id = ?",
        (orphan_id, assessment_id),
    )
    conn.execute("DELETE FROM persons WHERE person_id = ?", (result["person_id"],))
    conn.commit()
    conn.close()

    outcome = store.rename_assessment_person(assessment_id, "Max Schneider")

    row = _assessment(assessment_id)
    assert row["person_id"] == orphan_id  # unchanged, even though it self-healed
    assert outcome["person_id"] == orphan_id
    assert row["person_name"] == "Max Schneider"


# --- T3 / T4: Validate picks the terminal state from edited_at -------------


def test_t3_validate_without_edit_is_validated_ok(isolated_db):
    result = _save(isolated_db, _sheet())
    assessment_id = result["assessment_id"]

    new_status = store.finalize_assessment(assessment_id)

    assert new_status == "validated_ok"
    row = _assessment(assessment_id)
    assert row["status"] == "validated_ok"
    assert row["finalized_at"] is not None


def test_t4_validate_after_edit_is_validated_with_changes(isolated_db):
    result = _save(isolated_db, _sheet())
    assessment_id = result["assessment_id"]

    store.update_assessment_fields(assessment_id, {"klasse": "4a"})
    new_status = store.finalize_assessment(assessment_id)

    assert new_status == "validated_with_changes"
    assert _assessment(assessment_id)["status"] == "validated_with_changes"


# --- T5: Reject -------------------------------------------------------------


def test_t5_reject_sets_rejected_regardless_of_edits(isolated_db):
    result = _save(isolated_db, _sheet())
    assessment_id = result["assessment_id"]

    new_status = store.finalize_assessment(assessment_id, rejected=True)

    assert new_status == "rejected"
    assert _assessment(assessment_id)["status"] == "rejected"


# --- finalize_assessment refuses an already-terminal row -------------------


def test_finalize_assessment_refuses_terminal_row(isolated_db):
    result = _save(isolated_db, _sheet())
    assessment_id = result["assessment_id"]
    store.finalize_assessment(assessment_id)

    with pytest.raises(ValueError):
        store.finalize_assessment(assessment_id)


# --- mark_opened idempotence ------------------------------------------------


def test_mark_opened_is_idempotent(isolated_db):
    result = _save(isolated_db, _sheet())
    assessment_id = result["assessment_id"]

    store.mark_opened(assessment_id)
    first_opened_at = _assessment(assessment_id)["opened_at"]
    assert first_opened_at is not None

    store.mark_opened(assessment_id)
    assert _assessment(assessment_id)["opened_at"] == first_opened_at


# --- list_inbox_assessments: single list, created_at DESC, opening doesn't move it


def test_list_inbox_assessments_single_created_at_desc_sequence(isolated_db):
    first = _save(
        isolated_db, _sheet(child_name="Anna Schmidt"), page_key="p1", file_sha256="f1"
    )
    second = _save(
        isolated_db, _sheet(child_name="Ben Muller"), page_key="p2", file_sha256="f2"
    )
    third = _save(
        isolated_db, _sheet(child_name="Cara Klein"), page_key="p3", file_sha256="f3"
    )

    # created_at has second-resolution in the schema default; force a
    # deterministic order rather than relying on wall-clock ties.
    conn = sqlite3.connect(config.DB_PATH)
    conn.execute(
        "UPDATE assessments SET created_at = ? WHERE assessment_id = ?",
        ("2026-01-01T00:00:01", first["assessment_id"]),
    )
    conn.execute(
        "UPDATE assessments SET created_at = ? WHERE assessment_id = ?",
        ("2026-01-01T00:00:02", second["assessment_id"]),
    )
    conn.execute(
        "UPDATE assessments SET created_at = ? WHERE assessment_id = ?",
        ("2026-01-01T00:00:03", third["assessment_id"]),
    )
    conn.commit()
    conn.close()

    rows = store.list_inbox_assessments()
    ids_before = [r["assessment_id"] for r in rows]
    assert ids_before == [
        third["assessment_id"],
        second["assessment_id"],
        first["assessment_id"],
    ]

    # Opening the oldest (last) row must not move it: position is purely
    # created_at DESC, read/unread is styling only (ADR 0007).
    store.mark_opened(first["assessment_id"])

    rows_after = store.list_inbox_assessments()
    ids_after = [r["assessment_id"] for r in rows_after]
    assert ids_after == ids_before

    by_id = {r["assessment_id"]: r for r in rows_after}
    assert by_id[first["assessment_id"]]["opened_at"] is not None
    assert by_id[second["assessment_id"]]["opened_at"] is None
    assert by_id[third["assessment_id"]]["opened_at"] is None


def test_list_inbox_assessments_excludes_terminal_rows(isolated_db):
    result = _save(isolated_db, _sheet())
    store.finalize_assessment(result["assessment_id"])

    assert store.list_inbox_assessments() == []


# --- no-op form save leaves edited_at NULL ----------------------------------


def test_noop_field_update_call_leaves_edited_at_null(isolated_db):
    result = _save(isolated_db, _sheet())
    assessment_id = result["assessment_id"]

    # Mirrors app.py's diff-then-call-only-changed-updaters behavior: an
    # empty changes dict (nothing differed from the loaded values) must be
    # a true no-op, not stamp edited_at.
    store.update_assessment_fields(assessment_id, {})

    assert _assessment(assessment_id)["edited_at"] is None


# --- update_assessment_metric coercion matches save_assessment -------------


def test_update_assessment_metric_coercion_matches_save_assessment(isolated_db):
    sheet = _full_sheet_with_metric()
    result = _save(isolated_db, sheet)
    assessment_id = result["assessment_id"]

    metrics = store.get_assessment_metrics(assessment_id)
    rw_total = next(m for m in metrics if m["metric_key"] == "rw_total")
    assert rw_total["value_num"] == 13.0

    store.update_assessment_metric(assessment_id, rw_total["position"], "20,5")

    updated = store.get_assessment_metrics(assessment_id)
    rw_total_updated = next(m for m in updated if m["metric_key"] == "rw_total")
    assert rw_total_updated["value_raw"] == "20,5"
    assert rw_total_updated["value_num"] == 20.5  # German-comma coercion, ADR 0005 §6


def test_update_assessment_metric_unparseable_value_stores_none(isolated_db):
    sheet = _full_sheet_with_metric()
    result = _save(isolated_db, sheet)
    assessment_id = result["assessment_id"]
    metrics = store.get_assessment_metrics(assessment_id)
    rw_total = next(m for m in metrics if m["metric_key"] == "rw_total")

    store.update_assessment_metric(assessment_id, rw_total["position"], "not-a-number")

    updated = store.get_assessment_metrics(assessment_id)
    rw_total_updated = next(m for m in updated if m["metric_key"] == "rw_total")
    assert rw_total_updated["value_raw"] == "not-a-number"
    assert rw_total_updated["value_num"] is None


def test_update_subtest_score_edits_and_marks_edited(isolated_db):
    sheet = _full_sheet_with_metric()
    result = _save(isolated_db, sheet)
    assessment_id = result["assessment_id"]
    subtests = store.get_subtest_scores(assessment_id)
    sa = next(s for s in subtests if s["subtest_key"] == "SA")

    store.update_subtest_score(
        assessment_id, sa["position"], value_raw="4", rw=4.0, rw_max=4.0, pr=50
    )

    updated = store.get_subtest_scores(assessment_id)
    sa_updated = next(s for s in updated if s["subtest_key"] == "SA")
    assert sa_updated["rw"] == 4.0
    assert sa_updated["pr"] == 50
    assert _assessment(assessment_id)["edited_at"] is not None


# --- raw_response_json is immutable after an edit ---------------------------


def test_raw_response_json_unchanged_after_edit(isolated_db):
    sheet = _sheet(child_name="Dana Fischer")
    result = _save(isolated_db, sheet)
    assessment_id = result["assessment_id"]

    conn = sqlite3.connect(config.DB_PATH)
    before = conn.execute(
        "SELECT raw_response_json FROM assessments WHERE assessment_id = ?",
        (assessment_id,),
    ).fetchone()[0]
    conn.close()
    assert json.loads(before)["child_name"] == "Dana Fischer"

    store.update_assessment_fields(assessment_id, {"klasse": "4a"})

    conn = sqlite3.connect(config.DB_PATH)
    after = conn.execute(
        "SELECT raw_response_json FROM assessments WHERE assessment_id = ?",
        (assessment_id,),
    ).fetchone()[0]
    conn.close()
    assert after == before
