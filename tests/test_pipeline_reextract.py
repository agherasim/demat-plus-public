"""`pipeline.reextract_assessments` (ADR 0016): replace an already-extracted
row from its stored source page in one transaction, keeping the child link
and the Normtabelle, and leaving the old row byte-identical on any failure.

`extract_sheet` / `run_review_validations` / `norm_completion.run_completion`
are monkeypatched (the `test_pipeline_norm_completion.py` pattern) except in
the stale-run-output test, which drives the real `extract_sheet` with a fake
runner. Synthetic names only; the source PDFs are tiny blank pages.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import threading
from pathlib import Path

import pypdfium2
import pytest

from demat import agent_runner, config, identity, norm_completion, pdf_render, store
from demat import pipeline as pipeline_module
from demat.agent_runner import ExtractionStoppedError, RunnerUnavailableError
from demat.extraction import ExtractionResult
from demat.extraction import extract_sheet as real_extract_sheet
from demat.pipeline import (
    BatchIdentityTracker,
    _process_sheet,
    reextract_assessments,
)
from demat.review_validation import ReviewValidationResult
from demat.schema import ExtractedSheet, SubtestScore, TriageResult

TEST_SALT = "test-salt"


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "demat.db")
    monkeypatch.setattr(config, "PAGES_DIR", tmp_path / "pages")
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(config, "UPLOADS_DIR", tmp_path / "uploads")
    monkeypatch.setattr(config, "SALT_PATH", tmp_path / ".salt")
    monkeypatch.setattr(config, "STORE_RAW_NAMES", True)
    monkeypatch.setenv("DEMAT_ID_SALT", TEST_SALT)
    store.init_db()
    return tmp_path


class _DummyRunner:
    def run(self, *a, **k):
        raise AssertionError("must not be called: extract_sheet is monkeypatched")

    def run_text(self, *a, **k):
        raise AssertionError("must not be called: run_review_validations is stubbed")


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
        "subtests": [
            SubtestScore(key="ZE", rw="2", pr=None, pr_state="blank", confidence="high")
        ],
        "overall": [],
        "assessment_note": None,
        "illegible_notes": None,
        "overall_confidence": "high",
    }
    defaults.update(overrides)
    return ExtractedSheet(**defaults)


class FakeExtraction:
    """Controls what the monkeypatched `extract_sheet` returns. `outcome` is
    an `ExtractedSheet`, `"not_a_sheet"`, `"error"`, an exception instance,
    or a callable returning one of those."""

    def __init__(self) -> None:
        self.outcome: object = _sheet()
        self.calls: list[str] = []
        self.completion_ids: list[int] = []
        self.validation_ids: list[int] = []

    def __call__(self, runner, image_path, output_path_base, stop_event=None):
        self.calls.append(image_path)
        outcome = self.outcome
        if callable(outcome) and not isinstance(outcome, ExtractedSheet):
            outcome = outcome()
        if isinstance(outcome, BaseException):
            raise outcome
        if outcome == "not_a_sheet":
            return ExtractionResult(
                status="not_a_sheet",
                triage=TriageResult(is_demat_sheet=False, confidence="high"),
                extracted=None,
                skill_used=None,
                raw_response=None,
                unknown_version=False,
            )
        if outcome == "error":
            return ExtractionResult(
                status="error",
                triage=None,
                extracted=None,
                skill_used="demat-sheet-triage",
                raw_response=None,
                unknown_version=False,
                error="triage failed: boom",
            )
        assert isinstance(outcome, ExtractedSheet)
        definition = store.get_test_definition(outcome.test_version)
        return ExtractionResult(
            status="ok",
            triage=TriageResult(
                is_demat_sheet=True,
                test_version=outcome.test_version,
                test_form="A",
                confidence="high",
            ),
            extracted=outcome,
            skill_used="demat-2plus-extraction",
            raw_response=outcome.model_dump(),
            unknown_version=definition is None,
            definition=definition,
        )


@pytest.fixture
def fake(isolated_db, monkeypatch):
    """Patch extraction, completion and review validation; record the ids
    completion/validation ran on."""
    fake_extract = FakeExtraction()
    monkeypatch.setattr(pipeline_module, "extract_sheet", fake_extract)

    def spy_run_completion(assessment_id, **kwargs):
        fake_extract.completion_ids.append(assessment_id)
        return norm_completion.CompletionPlan()

    def spy_run_review_validations(assessment, *args, **kwargs):
        fake_extract.validation_ids.append(assessment["assessment_id"])
        return ReviewValidationResult(findings=[])

    monkeypatch.setattr(
        pipeline_module.norm_completion, "run_completion", spy_run_completion
    )
    monkeypatch.setattr(
        pipeline_module, "run_review_validations", spy_run_review_validations
    )
    return fake_extract


def _pdf_bytes(n_pages: int = 1, width: int = 200) -> bytes:
    pdf = pypdfium2.PdfDocument.new()
    try:
        for _ in range(n_pages):
            pdf.new_page(width, 300)
        buf = io.BytesIO()
        pdf.save(buf)
        return buf.getvalue()
    finally:
        pdf.close()


def _upload(n_pages: int = 1, width: int = 200) -> str:
    """Store a real PDF under UPLOADS_DIR/<sha>.pdf, as the Upload page
    does; return its sha."""
    pdf_bytes = _pdf_bytes(n_pages, width)
    sha = hashlib.sha256(pdf_bytes).hexdigest()
    store.save_uploaded_document(sha, "test.pdf", pdf_bytes)
    return sha


def _seed(fake, sha: str, page_index: int = 0, sheet=None, norm_variant=None, **kw):
    """Save a row the way an upload does (`_process_sheet`, no replace)."""
    page = pdf_render.render_page(
        (config.UPLOADS_DIR / f"{sha}.pdf").read_bytes(), sha, page_index
    )
    fake.outcome = sheet or _sheet()
    result = _process_sheet(
        page,
        sha,
        _DummyRunner(),
        TEST_SALT,
        {sha: norm_variant} if norm_variant else {},
        None,
        kw.get("tracker") or BatchIdentityTracker(),
    )
    assert result.status == "saved", result.error
    assert result.assessment_id is not None
    fake.calls.clear()
    fake.completion_ids.clear()
    fake.validation_ids.clear()
    return result.assessment_id


def _row(aid: int | None) -> dict:
    assert aid is not None
    row = store.get_assessment(aid)
    assert row is not None
    return row


def _target(aid: int) -> dict:
    target = store.get_reextract_target(aid)
    assert target is not None
    return target


def _snapshot(aid: int):
    return (
        store.get_assessment(aid),
        store.get_assessment_metrics(aid),
        store.get_subtest_scores(aid),
    )


def _owners(page_key: str) -> list[int]:
    with store.get_connection() as conn:
        rows = conn.execute(
            "SELECT assessment_id FROM assessments WHERE source_page_key = ?",
            (page_key,),
        ).fetchall()
    return [r["assessment_id"] for r in rows]


def _count(table: str) -> int:
    with store.get_connection() as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _assert_unchanged(aid: int, before) -> None:
    assert _snapshot(aid) == before
    page_key = _target(aid)["source_page_key"]
    assert _owners(page_key) == [aid]


# --- Happy path ------------------------------------------------------------


def test_replaces_row_with_new_id_keeping_person_and_variant(fake):
    sha = _upload()
    old = _seed(fake, sha, norm_variant="k2")
    old_row = _row(old)
    store.mark_opened(old)
    (config.RUNS_DIR / f"review_validation_{old}").mkdir(parents=True)

    fake.outcome = _sheet(
        subtests=[
            SubtestScore(key="ZE", rw="3", pr="40", confidence="high"),
            SubtestScore(key="AD", rw="1", pr="10", confidence="high"),
        ]
    )
    results = reextract_assessments([old], _DummyRunner())

    assert len(results) == 1
    r = results[0]
    assert r.status == "replaced"
    assert r.new_assessment_id is not None and r.new_assessment_id != old
    new = r.new_assessment_id

    assert store.get_assessment(old) is None
    with store.get_connection() as conn:
        for table in ("assessment_metrics", "subtest_scores"):
            n = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE assessment_id = ?", (old,)
            ).fetchone()[0]
            assert n == 0

    new_row = _row(new)
    assert new_row["status"] == "needs_review"
    assert new_row["opened_at"] is None
    assert new_row["person_id"] == old_row["person_id"] == r.person_id
    assert new_row["norm_variant"] == "k2"
    target = _target(new)
    assert _owners(target["source_page_key"]) == [new]
    assert (
        f"re-extracted from assessment #{old} (was needs_review)"
        in (new_row["review_notes"])
    )
    assert "not carried over" in new_row["review_notes"]
    assert "duplicate_suspected" not in new_row["flags"]
    assert "reextract_name_mismatch" not in new_row["flags"]
    assert [s["subtest_key"] for s in store.get_subtest_scores(new)] == ["ZE", "AD"]

    assert fake.completion_ids == [new]
    assert fake.validation_ids == [new]
    assert not (config.RUNS_DIR / f"review_validation_{old}").exists()
    assert _count("persons") == 1


def test_audit_note_records_edited_and_finalized_without_names(fake):
    sha = _upload()
    old = _seed(fake, sha)
    store.update_assessment_fields(old, {"klasse": "3D"})
    store.finalize_assessment(old)
    finalized_at = _row(old)["finalized_at"]

    [r] = reextract_assessments([old], _DummyRunner())

    notes = _row(r.new_assessment_id)["review_notes"]
    assert (
        f"re-extracted from assessment #{old} (was validated_with_changes, edited, "
        f"finalized {finalized_at}); prior manual edits and review findings were "
        "not carried over"
    ) in notes
    assert "Max" not in notes.split("re-extracted", 1)[1]


def test_stored_pdf_is_rerendered_for_exactly_that_page(fake, monkeypatch):
    sha = _upload(n_pages=3)
    old = _seed(fake, sha, page_index=2)
    page_key = pdf_render._page_key(sha, 2)
    png = config.PAGES_DIR / f"{page_key}.png"
    png.unlink()

    calls = []
    real_render_page = pdf_render.render_page

    def spy(pdf_bytes, file_sha256, page_index, *a, **k):
        calls.append((file_sha256, page_index))
        return real_render_page(pdf_bytes, file_sha256, page_index, *a, **k)

    monkeypatch.setattr(pdf_render, "render_page", spy)

    [r] = reextract_assessments([old], _DummyRunner())

    assert r.status == "replaced"
    assert calls == [(sha, 2)]
    assert png.exists()
    assert fake.calls == [str(png)]


# --- Failure paths: the old row is unchanged -------------------------------


def test_not_a_sheet_keeps_row(fake):
    old = _seed(fake, _upload())
    before = _snapshot(old)
    fake.outcome = "not_a_sheet"

    [r] = reextract_assessments([old], _DummyRunner())

    assert r.status == "kept_not_a_sheet"
    _assert_unchanged(old, before)


def test_extraction_error_keeps_row(fake):
    old = _seed(fake, _upload())
    before = _snapshot(old)
    fake.outcome = "error"

    [r] = reextract_assessments([old], _DummyRunner())

    assert r.status == "kept_error"
    assert r.error == "triage failed: boom"
    _assert_unchanged(old, before)


def test_stopped_mid_run_marks_this_and_remaining_rows_stopped(fake):
    ids = [_seed(fake, _upload(width=200 + i)) for i in range(3)]
    snapshots = [_snapshot(aid) for aid in ids]
    fake.outcome = ExtractionStoppedError("stopped")

    results = reextract_assessments(ids, _DummyRunner())

    assert [r.status for r in results] == ["stopped"] * 3
    assert [r.old_assessment_id for r in results] == ids
    assert len(fake.calls) == 1
    for aid, before in zip(ids, snapshots, strict=True):
        _assert_unchanged(aid, before)


def test_stop_event_set_before_call_processes_nothing(fake):
    ids = [_seed(fake, _upload(width=200 + i)) for i in range(2)]
    snapshots = [_snapshot(aid) for aid in ids]
    stop = threading.Event()
    stop.set()

    results = reextract_assessments(ids, _DummyRunner(), stop_event=stop)

    assert [r.status for r in results] == ["stopped", "stopped"]
    assert fake.calls == []
    for aid, before in zip(ids, snapshots, strict=True):
        _assert_unchanged(aid, before)


def test_runner_unavailable_aborts_after_publishing_finished_rows(fake):
    ids = [_seed(fake, _upload(width=200 + i)) for i in range(3)]
    snapshots = [_snapshot(aid) for aid in ids]
    n = {"calls": 0}

    def outcome():
        n["calls"] += 1
        if n["calls"] == 2:
            return RunnerUnavailableError("no claude CLI")
        return _sheet()

    fake.outcome = outcome
    sink: list = []
    progress: list = []

    with pytest.raises(RunnerUnavailableError):
        reextract_assessments(
            ids,
            _DummyRunner(),
            results_sink=sink,
            progress_cb=lambda *a: progress.append(a),
        )

    assert len(sink) == 1
    assert sink[0].old_assessment_id == ids[0]
    assert sink[0].status == "replaced"
    assert progress == [(1, 3, ids[0])]
    assert store.get_assessment(ids[0]) is None
    for aid, before in zip(ids[1:], snapshots[1:], strict=True):
        _assert_unchanged(aid, before)


def test_failure_inside_the_transaction_rolls_back_the_delete(fake, monkeypatch):
    old = _seed(fake, _upload())
    before = _snapshot(old)
    n_before = _count("assessments")
    fake.outcome = _sheet(
        subtests=[SubtestScore(key="ZE", rw="3", pr="40", confidence="high")]
    )

    def boom(*a, **k):
        raise RuntimeError("subtest insert failed")

    # Runs inside save_assessment's subtest loop — after the DELETE and the
    # INSERT, in the same transaction.
    monkeypatch.setattr(store.test_registry, "canonical_subtest_key", boom)

    [r] = reextract_assessments([old], _DummyRunner())

    assert r.status == "kept_error"
    assert "subtest insert failed" in (r.error or "")
    assert _count("assessments") == n_before
    _assert_unchanged(old, before)
    assert fake.completion_ids == []


def test_row_deleted_during_extraction_is_not_resurrected(fake):
    old = _seed(fake, _upload())

    def outcome():
        store.delete_assessments([old])
        return _sheet()

    fake.outcome = outcome

    [r] = reextract_assessments([old], _DummyRunner())

    assert r.status == "kept_error"
    assert "no longer exists or no longer owns page" in (r.error or "")
    assert _count("assessments") == 0


class _WritesNothingRunner:
    """Mirrors the real runners' tail: whatever happened, read the output
    file. This one never writes it."""

    def __init__(self) -> None:
        self.saw_existing: list[tuple[str, bool]] = []

    def run(
        self,
        skill_name,
        image_path,
        output_path,
        extra_instructions=None,
        context=None,
        stop_event=None,
    ):
        self.saw_existing.append((output_path, Path(output_path).exists()))
        return agent_runner._read_output_json(output_path)

    def run_text(self, *a, **k):
        raise AssertionError("run_review_validations is stubbed")


def _seed_stale_run_outputs(page_key: str) -> tuple[Path, Path]:
    config.RUNS_DIR.mkdir(parents=True, exist_ok=True)
    triage = config.RUNS_DIR / f"{page_key}.triage.json"
    extraction = config.RUNS_DIR / f"{page_key}.json"
    triage.write_text(
        json.dumps(
            {
                "is_demat_sheet": True,
                "test_version": "DEMAT 2+",
                "test_form": "A",
                "confidence": "high",
            }
        )
    )
    extraction.write_text(_sheet(klasse="9Z").model_dump_json())
    return triage, extraction


def test_stale_run_outputs_are_cleared_so_a_silent_run_fails(fake, monkeypatch):
    old = _seed(fake, _upload())
    before = _snapshot(old)
    page_key = _target(old)["source_page_key"]
    triage, extraction = _seed_stale_run_outputs(page_key)
    monkeypatch.setattr(pipeline_module, "extract_sheet", real_extract_sheet)
    runner = _WritesNothingRunner()

    [r] = reextract_assessments([old], runner)

    assert r.status == "kept_error"
    assert "triage failed" in (r.error or "")
    assert runner.saw_existing and runner.saw_existing[0] == (str(triage), False)
    assert not triage.exists() and not extraction.exists()
    _assert_unchanged(old, before)


def test_control_without_clearing_the_stale_payload_is_saved_as_new(fake, monkeypatch):
    """Proves the ADR 0016 §3 hazard is real: without the unlink, a runner
    that writes nothing 'succeeds' with the previous run's file."""
    old = _seed(fake, _upload())
    page_key = _target(old)["source_page_key"]
    _seed_stale_run_outputs(page_key)
    monkeypatch.setattr(pipeline_module, "extract_sheet", real_extract_sheet)
    monkeypatch.setattr(store, "clear_run_outputs", lambda page_key: None)

    [r] = reextract_assessments([old], _WritesNothingRunner())

    assert r.status == "replaced"
    assert _row(r.new_assessment_id)["klasse"] == "9Z"


# --- Source resolution -----------------------------------------------------


def _set_stored_pdf_path(sha: str, path: str | None) -> None:
    with store.get_connection() as conn:
        conn.execute(
            "UPDATE source_files SET stored_pdf_path = ? WHERE file_sha256 = ?",
            (path, sha),
        )


def test_moved_home_falls_back_to_uploads_copy(fake, monkeypatch):
    sha = _upload()
    old = _seed(fake, sha)
    _set_stored_pdf_path(sha, "/nonexistent/old-home/uploads/x.pdf")
    calls = []
    real_render_page = pdf_render.render_page

    def spy(pdf_bytes, file_sha256, page_index, *a, **k):
        calls.append(page_index)
        return real_render_page(pdf_bytes, file_sha256, page_index)

    monkeypatch.setattr(pdf_render, "render_page", spy)

    [r] = reextract_assessments([old], _DummyRunner())

    assert r.status == "replaced"
    assert calls == [0]


def test_pdf_with_wrong_sha_is_rejected_and_png_used(fake, monkeypatch):
    sha = _upload()
    old = _seed(fake, sha)
    (config.UPLOADS_DIR / f"{sha}.pdf").write_bytes(_pdf_bytes(1, width=999))
    png = config.PAGES_DIR / f"{pdf_render._page_key(sha, 0)}.png"
    png_bytes = png.read_bytes()
    monkeypatch.setattr(
        pdf_render,
        "render_page",
        lambda *a, **k: pytest.fail("a sha-mismatched PDF must not be rendered"),
    )

    [r] = reextract_assessments([old], _DummyRunner())

    assert r.status == "replaced"
    assert fake.calls == [str(png)]
    assert png.read_bytes() == png_bytes


def test_no_pdf_and_no_png_is_skipped_untouched(fake):
    sha = _upload()
    old = _seed(fake, sha)
    before = _snapshot(old)
    (config.UPLOADS_DIR / f"{sha}.pdf").unlink()
    Path(before[0]["page_image_path"]).unlink()

    [r] = reextract_assessments([old], _DummyRunner())

    assert r.status == "skipped_source_unavailable"
    assert "not found" in (r.error or "")
    assert fake.calls == []
    _assert_unchanged(old, before)


def test_page_index_out_of_range_uses_png_or_skips(fake):
    sha = _upload()
    old = _seed(fake, sha)
    # Point the row at page 5 of a 1-page PDF (consistent page key, so
    # only the index range check can reject the PDF).
    bad_key = pdf_render._page_key(sha, 5)
    png = config.PAGES_DIR / f"{bad_key}.png"
    with store.get_connection() as conn:
        conn.execute(
            "UPDATE assessments SET source_page_index = 5, source_page_key = ?, "
            "page_image_path = ? WHERE assessment_id = ?",
            (bad_key, str(png), old),
        )
    before = _snapshot(old)

    [r] = reextract_assessments([old], _DummyRunner())
    assert r.status == "skipped_source_unavailable"
    assert "out of range" in (r.error or "")
    _assert_unchanged(old, before)

    png.write_bytes(b"fake png")
    [r] = reextract_assessments([old], _DummyRunner())
    assert r.status == "replaced"
    assert fake.calls == [str(png)]


def test_null_source_sha_is_skipped_even_with_a_png(fake):
    sha = _upload()
    old = _seed(fake, sha)
    with store.get_connection() as conn:
        conn.execute(
            "UPDATE assessments SET source_file_sha256 = NULL WHERE assessment_id = ?",
            (old,),
        )
    before = _snapshot(old)
    assert Path(before[0]["page_image_path"]).exists()

    [r] = reextract_assessments([old], _DummyRunner())

    assert r.status == "skipped_source_unavailable"
    assert fake.calls == []
    _assert_unchanged(old, before)


def test_unknown_id_is_skipped_and_duplicate_ids_processed_once(fake):
    old = _seed(fake, _upload())

    results = reextract_assessments([old, 99999, old], _DummyRunner())

    assert [(r.old_assessment_id, r.status) for r in results] == [
        (old, "replaced"),
        (99999, "skipped_missing"),
    ]
    assert len(fake.calls) == 1


# --- Identity --------------------------------------------------------------


def _person_names() -> list[tuple[str, str]]:
    with store.get_connection() as conn:
        return [
            (r["person_id"], r["raw_name"])
            for r in conn.execute("SELECT person_id, raw_name FROM person_names")
        ]


def test_kept_link_same_name_no_new_person_no_flag(fake):
    old = _seed(fake, _upload())
    pid = _row(old)["person_id"]

    [r] = reextract_assessments([old], _DummyRunner())

    assert r.person_id == pid
    assert "reextract_name_mismatch" not in r.flags
    assert _count("persons") == 1


@pytest.mark.parametrize("store_raw_names", [True, False])
def test_kept_link_different_name_flags_mismatch(fake, monkeypatch, store_raw_names):
    old = _seed(fake, _upload())
    pid = _row(old)["person_id"]
    names_before = _person_names()
    monkeypatch.setattr(config, "STORE_RAW_NAMES", store_raw_names)
    fake.outcome = _sheet(child_name="Moritz Musterfrau")

    [r] = reextract_assessments([old], _DummyRunner())

    assert r.status == "replaced"
    assert r.person_id == pid
    assert "reextract_name_mismatch" in r.flags
    assert r.has_blocking_flags is True
    assert _count("persons") == 1
    assert _person_names() == names_before
    notes = _row(r.new_assessment_id)["review_notes"]
    assert f"reextract_name_mismatch: kept person_id={pid}" in notes
    assert ("Moritz" in notes) is store_raw_names


def test_kept_link_matches_a_corrected_spelling(fake):
    old = _seed(fake, _upload(), sheet=_sheet(child_name="Max Musterman"))
    pid = _row(old)["person_id"]
    store.rename_assessment_person(old, "Max Mustermann")
    fake.outcome = _sheet(child_name="Max Mustermann")

    [r] = reextract_assessments([old], _DummyRunner())

    assert r.person_id == pid
    assert "reextract_name_mismatch" not in r.flags


def test_kept_disambiguated_person_is_not_remerged(fake):
    old = _seed(fake, _upload(), sheet=_sheet(child_name=None))
    with store.get_connection() as conn:
        conn.execute(
            "INSERT INTO persons (person_id, normalization_key) VALUES (?, ?)",
            ("XYZE", "anna beispiel#E"),
        )
        conn.execute(
            "UPDATE assessments SET person_id = 'XYZE' WHERE assessment_id = ?",
            (old,),
        )
    fake.outcome = _sheet(child_name="Anna Beispiel")

    [r] = reextract_assessments([old], _DummyRunner())

    assert r.person_id == "XYZE"
    assert "reextract_name_mismatch" not in r.flags
    assert _count("persons") == 1


def test_other_assessment_same_test_and_date_raises_duplicate(fake):
    a = _seed(fake, _upload(width=201))
    b = _seed(fake, _upload(width=202))
    assert _row(a)["person_id"] == _row(b)["person_id"]

    [r] = reextract_assessments([a], _DummyRunner())

    assert "duplicate_suspected" in r.flags


def test_gender_mismatch_with_other_assessment_flags_merge(fake):
    other_date = {"test_date_iso": "2025-01-01", "test_date_raw": "01.01.25"}
    a = _seed(fake, _upload(width=201), sheet=_sheet(gender="maedchen"))
    b = _seed(fake, _upload(width=202), sheet=_sheet(gender="maedchen", **other_date))
    fake.outcome = _sheet(gender="junge", **other_date)

    [r] = reextract_assessments([b], _DummyRunner())

    assert r.person_id == _row(a)["person_id"]
    assert "possible_person_merge" in r.flags
    assert "duplicate_suspected" not in r.flags


def test_unlinked_rows_resolve_normally_with_one_shared_tracker(fake):
    ids = [
        _seed(fake, _upload(width=200 + i), sheet=_sheet(child_name=None))
        for i in range(2)
    ]
    fake.outcome = _sheet(child_name="Anna Beispiel")

    results = reextract_assessments(ids, _DummyRunner())

    expected_id, _ = identity.person_id_for("Anna Beispiel", TEST_SALT)
    assert results[0].person_id == expected_id
    assert results[1].person_id is not None
    assert results[1].person_id != expected_id
    assert _count("persons") == 2


def test_kept_person_deleted_meanwhile_falls_back_to_computed(fake, caplog):
    old = _seed(fake, _upload())
    pid = _row(old)["person_id"]

    def outcome():
        with store.get_connection() as conn:
            conn.execute("DELETE FROM persons WHERE person_id = ?", (pid,))
        return _sheet()

    fake.outcome = outcome
    with caplog.at_level(logging.WARNING, logger="demat.store"):
        [r] = reextract_assessments([old], _DummyRunner())

    assert r.status == "replaced"
    expected_id, _ = identity.person_id_for("Max Mustermann", TEST_SALT)
    assert r.person_id == expected_id
    assert any("no longer exists" in rec.message for rec in caplog.records)


# --- Norm variant ----------------------------------------------------------


def test_valid_carried_variant_is_kept(fake):
    old = _seed(fake, _upload(), norm_variant="k3")

    [r] = reextract_assessments([old], _DummyRunner())

    assert _row(r.new_assessment_id)["norm_variant"] == "k3"


def test_variant_invalid_for_new_test_is_unset_with_note(fake):
    old = _seed(fake, _upload(), norm_variant="k2")
    fake.outcome = _sheet(test_version="DEMAT 3+", subtests=[])

    [r] = reextract_assessments([old], _DummyRunner())

    assert r.status == "replaced"
    row = _row(r.new_assessment_id)
    assert row["norm_variant"] is None
    assert (
        "norm_variant 'k2' not carried over: not valid for 'demat3plus'"
        in (row["review_notes"])
    )


# --- Regression ------------------------------------------------------------


def test_upload_saved_sheet_result_carries_assessment_id(fake):
    sha = _upload()
    aid = _seed(fake, sha)
    assert store.get_assessment(aid) is not None
