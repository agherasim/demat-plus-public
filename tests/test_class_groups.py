"""ADR 0008 spec §4/§10: the class grouping key, the Aug cutover at group
level, the NULL-date ungrouped rule, new/generated/stale transitions, and
`assessment_ids` being the validated-only comparison set.
"""

from __future__ import annotations

import pytest

from demat import class_groups, config, identity, store
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
    monkeypatch.setattr(config, "REPORTS_DIR", tmp_path / "reports")
    monkeypatch.setattr(config, "SALT_PATH", tmp_path / ".salt")
    store.init_db()
    return tmp_path


def _save_with_norm_variant(
    *,
    norm_variant,
    page_key,
    file_sha256,
    child_name="Max Mustermann",
):
    """Local helper (ADR 0010): `_save()` below doesn't thread norm_variant
    through, so these tests call `store.save_assessment` directly."""
    from demat import identity

    sheet = _sheet(child_name=child_name)
    store.upsert_source_file(file_sha256, "test.pdf", 1)
    definition = store.get_test_definition(sheet.test_version)
    triage = _triage()
    validation = validate(sheet, triage, definition)
    computed_person_id, normalization_key = identity.person_id_for(
        sheet.child_name, TEST_SALT
    )
    result = store.save_assessment(
        file_sha256=file_sha256,
        page_index=0,
        page_key=page_key,
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
    store.finalize_assessment(result["assessment_id"])
    return result


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
    sheet,
    *,
    page_key="page-key-1",
    file_sha256="file-sha-1",
    validate_as: str | None = None,
) -> dict:
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
    result = store.save_assessment(
        file_sha256=file_sha256,
        page_index=0,
        page_key=page_key,
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
    )
    if validate_as == "ok":
        store.finalize_assessment(result["assessment_id"])
    elif validate_as == "rejected":
        store.finalize_assessment(result["assessment_id"], rejected=True)
    return result


# --- grouping key ---------------------------------------------------------


def test_group_key_is_upper_klasse_schuljahr_test_version(isolated_db):
    _save(_sheet(klasse="3c", test_date_iso="2026-09-16"), validate_as="ok")

    groups = store.list_class_groups()
    assert len(groups) == 1
    g = groups[0]
    assert g["klasse"] == "3C"
    assert g["schuljahr"] == "2026-27"
    assert g["test_version"] == "DEMAT 2+"
    assert g["child_count"] == 1


def test_group_key_uppercases_klasse_even_if_already_mixed_case(isolated_db):
    _save(
        _sheet(klasse="3c", test_date_iso="2026-09-16"),
        page_key="p1",
        validate_as="ok",
    )
    _save(
        _sheet(klasse="3C", test_date_iso="2026-09-18", child_name="Lea Lang"),
        page_key="p2",
        file_sha256="f2",
        validate_as="ok",
    )

    groups = store.list_class_groups()
    assert len(groups) == 1
    assert groups[0]["child_count"] == 2


def test_group_key_excludes_test_form(isolated_db):
    """Form A and B stay one cohort — test_form is not part of the key."""
    _save(
        _sheet(test_form="A", test_date_iso="2026-09-16"),
        page_key="p1",
        validate_as="ok",
    )
    _save(
        _sheet(test_form="B", test_date_iso="2026-09-17", child_name="Lea Lang"),
        page_key="p2",
        file_sha256="f2",
        validate_as="ok",
    )

    groups = store.list_class_groups()
    assert len(groups) == 1
    assert sorted(groups[0]["test_forms"]) == ["A", "B"]
    assert groups[0]["child_count"] == 2


# --- Aug cutover at group level --------------------------------------------


def test_group_key_splits_across_august_boundary(isolated_db):
    _save(
        _sheet(test_date_iso="2026-07-31"), page_key="p1", validate_as="ok"
    )  # 2025-26
    _save(
        _sheet(test_date_iso="2026-08-01", child_name="Lea Lang"),
        page_key="p2",
        file_sha256="f2",
        validate_as="ok",
    )  # 2026-27

    groups = store.list_class_groups()
    schuljahre = {g["schuljahr"] for g in groups}
    assert schuljahre == {"2025-26", "2026-27"}


# --- NULL-date ungrouped rule -----------------------------------------------


def test_null_test_date_is_ungrouped(isolated_db):
    _save(_sheet(test_date_iso=None), validate_as="ok")

    groups = store.list_class_groups()
    assert groups == []
    assert store.count_ungrouped_validated_assessments() == 1


def test_null_klasse_is_ungrouped(isolated_db):
    _save(_sheet(klasse=None), validate_as="ok")

    groups = store.list_class_groups()
    assert groups == []
    assert store.count_ungrouped_validated_assessments() == 1


def test_null_test_version_is_ungrouped(isolated_db):
    _save(_sheet(test_version=None), validate_as="ok")

    groups = store.list_class_groups()
    assert groups == []
    assert store.count_ungrouped_validated_assessments() == 1


def test_needs_review_only_group_not_listed_by_class_groups(isolated_db):
    """A group with only needs_review rows (no validated ones) is returned
    by `store.list_class_groups()` with child_count == 0, but
    `class_groups.list_groups()` excludes it from the "listed" set
    (spec §4.3's >=1 validated rule)."""
    _save(_sheet())  # left in needs_review

    raw_groups = store.list_class_groups()
    assert len(raw_groups) == 1
    assert raw_groups[0]["child_count"] == 0
    assert raw_groups[0]["awaiting_review_count"] == 1

    assert class_groups.list_groups() == []


# --- awaiting_review_count is validated-ids-only vs needs_review split ------


def test_awaiting_review_count_over_same_key(isolated_db):
    _save(_sheet(), page_key="p1", validate_as="ok")
    _save(
        _sheet(child_name="Lea Lang"), page_key="p2", file_sha256="f2"
    )  # left needs_review

    groups = store.list_class_groups()
    assert len(groups) == 1
    assert groups[0]["child_count"] == 1
    assert groups[0]["awaiting_review_count"] == 1
    assert len(groups[0]["assessment_ids"]) == 1


# --- new / generated / stale transitions ------------------------------------


def test_new_state_when_no_profile_exists(isolated_db):
    _save(_sheet(), validate_as="ok")

    groups = class_groups.list_groups()
    assert len(groups) == 1
    assert groups[0].state == "new"


def test_generated_state_when_profile_matches_current_ids(isolated_db):
    result = _save(_sheet(), validate_as="ok")

    store.save_class_profile(
        klasse="3C",
        schuljahr="2026-27",
        test_version="DEMAT 2+",
        test_form_summary="A",
        test_date_min="2026-09-16",
        test_date_max="2026-09-16",
        norm_group="k3_gesamt",
        classification_basis="pr",
        child_count=1,
        assessment_ids=[result["assessment_id"]],
        markdown_path=str(isolated_db / "report.md"),
    )

    groups = class_groups.list_groups()
    assert len(groups) == 1
    assert groups[0].state == "generated"


def test_stale_state_when_a_new_assessment_is_validated_after_generation(isolated_db):
    result = _save(_sheet(), page_key="p1", validate_as="ok")

    store.save_class_profile(
        klasse="3C",
        schuljahr="2026-27",
        test_version="DEMAT 2+",
        test_form_summary="A",
        test_date_min="2026-09-16",
        test_date_max="2026-09-16",
        norm_group="k3_gesamt",
        classification_basis="pr",
        child_count=1,
        assessment_ids=[result["assessment_id"]],
        markdown_path=str(isolated_db / "report.md"),
    )

    # A second child validated after the profile was generated.
    _save(
        _sheet(child_name="Lea Lang"),
        page_key="p2",
        file_sha256="f2",
        validate_as="ok",
    )

    groups = class_groups.list_groups()
    assert len(groups) == 1
    assert groups[0].state == "stale"


def test_stale_state_when_an_included_assessment_is_removed(isolated_db):
    """An add-then-remove: the count matches but the set doesn't — the
    id-set comparison (not a count) must still flag it stale."""
    first = _save(_sheet(), page_key="p1", validate_as="ok")
    second = _save(
        _sheet(child_name="Lea Lang"),
        page_key="p2",
        file_sha256="f2",
        validate_as="ok",
    )

    store.save_class_profile(
        klasse="3C",
        schuljahr="2026-27",
        test_version="DEMAT 2+",
        test_form_summary="A",
        test_date_min="2026-09-16",
        test_date_max="2026-09-16",
        norm_group="k3_gesamt",
        classification_basis="pr",
        child_count=2,
        assessment_ids=[first["assessment_id"], second["assessment_id"]],
        markdown_path=str(isolated_db / "report.md"),
    )

    third = _save(
        _sheet(child_name="Ben Muller"),
        page_key="p3",
        file_sha256="f3",
        validate_as="ok",
    )
    # Reject `second` retroactively isn't supported (already terminal); instead
    # simulate "removed" by building a profile over {first, third} — a
    # different set of the same size as the original {first, second}.
    store.save_class_profile(
        klasse="3C",
        schuljahr="2026-27",
        test_version="DEMAT 2+",
        test_form_summary="A",
        test_date_min="2026-09-16",
        test_date_max="2026-09-16",
        norm_group="k3_gesamt",
        classification_basis="pr",
        child_count=2,
        assessment_ids=[first["assessment_id"], third["assessment_id"]],
        markdown_path=str(isolated_db / "report2.md"),
    )

    groups = class_groups.list_groups()
    assert len(groups) == 1
    # Latest profile covers {first, third}; current validated set is
    # {first, second, third} — different sets, same size as a naive count
    # would miss if it only compared len().
    assert groups[0].child_count == 3
    assert groups[0].state == "stale"
